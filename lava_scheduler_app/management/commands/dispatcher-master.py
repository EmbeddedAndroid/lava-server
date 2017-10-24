# Copyright (C) 2015 Linaro Limited
#
# Author: Remi Duraffort <remi.duraffort@linaro.org>
#
# This file is part of LAVA Dispatcher.
#
# LAVA Dispatcher is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# LAVA Dispatcher is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along
# with this program; if not, see <http://www.gnu.org/licenses>.

# pylint: disable=wrong-import-order

import errno
import sys
import fcntl
import jinja2
import logging
import lzma
import os
import signal
import time
import yaml
import zmq
import zmq.auth
from zmq.auth.thread import ThreadAuthenticator
from threading import Thread

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.db.utils import OperationalError, InterfaceError
from lava_scheduler_app.models import TestJob
from lava_scheduler_app.utils import mkdir
from lava_scheduler_app.dbutils import (
    create_job, start_job,
    fail_job, cancel_job,
    parse_job_description,
    select_device,
)
from lava_results_app.dbutils import map_scanned_results, create_metadata_store


# pylint: disable=no-member,too-many-branches,too-many-statements,too-many-locals

# Current version of the protocol
# The slave does send the protocol version along with the HELLO and HELLO_RETRY
# messages. If both version are not identical, the connection is refused by the
# master.
PROTOCOL_VERSION = 1
# TODO constants to move into external files
FD_TIMEOUT = 60
TIMEOUT = 10
DB_LIMIT = 10

# TODO: share this value with dispatcher-slave
# This should be 3 times the slave ping timeout
DISPATCHER_TIMEOUT = 3 * 10


class SlaveDispatcher(object):  # pylint: disable=too-few-public-methods

    def __init__(self, hostname, online=False):
        self.hostname = hostname
        self.last_msg = time.time() if online else 0
        self.online = online

    def alive(self):
        self.last_msg = time.time()


class JobHandler(object):  # pylint: disable=too-few-public-methods
    def __init__(self, job, level, filename):
        self.output_dir = job.output_dir
        self.output = open(os.path.join(self.output_dir, 'output.yaml'), 'a+')
        self.current_level = level
        self.sub_log = open(filename, 'a+')
        self.last_usage = time.time()

    def write(self, message):
        self.output.write(message)
        self.output.write('\n')
        self.output.flush()
        self.sub_log.write(message)
        self.sub_log.write('\n')
        self.sub_log.flush()

    def close(self):
        self.output.close()
        self.sub_log.close()


def load_optional_yaml_file(filename):
    """
    Returns the string after checking for YAML errors which would cause issues later.
    Only raise an error if the file exists but is not readable or parsable
    """
    try:
        with open(filename, "r") as f_in:
            data_str = f_in.read()
        yaml.safe_load(data_str)
        return data_str
    except IOError as exc:
        # This is ok if the file does not exist
        if exc.errno == errno.ENOENT:
            return ''
        raise
    except yaml.YAMLError:
        # Raise an IOError because the caller uses yaml.YAMLError for a
        # specific usage. Allows here to specify the faulty filename.
        raise IOError("", "Not a valid YAML file", filename)


class Command(BaseCommand):
    """
    worker_host is the hostname of the worker this field is set by the admin
    and could therefore be empty in a misconfigured instance.
    """
    logger = None
    help = "LAVA dispatcher master"

    def __init__(self, *args, **options):
        super(Command, self).__init__(*args, **options)
        self.pull_socket = None
        self.controler = None
        # List of logs
        self.jobs = {}
        # List of known dispatchers. At startup do not load this from the
        # database. This will help to know if the slave as restarted or not.
        self.dispatchers = {}
        self.logging_support()

    def add_arguments(self, parser):
        parser.add_argument('--master-socket',
                            default='tcp://*:5556',
                            help="Socket for master-slave communication. Default: tcp://*:5556")
        parser.add_argument('--log-socket',
                            default='tcp://*:5555',
                            help="Socket waiting for logs. Default: tcp://*:5555")
        parser.add_argument('--encrypt', default=False, action='store_true',
                            help="Encrypt messages")
        parser.add_argument('--master-cert',
                            default='/etc/lava-dispatcher/certificates.d/master.key_secret',
                            help="Certificate for the master socket")
        parser.add_argument('--slaves-certs',
                            default='/etc/lava-dispatcher/certificates.d',
                            help="Directory for slaves certificates")
        parser.add_argument('-l', '--level',
                            default='DEBUG',
                            help="Logging level (ERROR, WARN, INFO, DEBUG) Default: DEBUG")
        parser.add_argument('--templates',
                            default="/etc/lava-server/dispatcher-config/",
                            help="Base directory for device configuration templates. "
                                 "Default: /etc/lava-server/dispatcher-config/")
        # Important: ensure share/env.yaml is put into /etc/ by setup.py in packaging.
        parser.add_argument('--env',
                            default="/etc/lava-server/env.yaml",
                            help="Environment variables for the dispatcher processes. "
                                 "Default: /etc/lava-server/env.yaml")
        parser.add_argument('--env-dut',
                            default="/etc/lava-server/env.dut.yaml",
                            help="Environment variables for device under test. "
                                 "Default: /etc/lava-server/env.dut.yaml")
        parser.add_argument('--dispatchers-config',
                            default="/etc/lava-server/dispatcher.d",
                            help="Directory that might contain dispatcher specific configuration")

    def send_status(self, hostname):
        """
        The master crashed, send a STATUS message to get the current state of jobs
        """
        jobs = TestJob.objects.filter(actual_device__worker_host__hostname=hostname,
                                      is_pipeline=True,
                                      status=TestJob.RUNNING)
        for job in jobs:
            self.logger.info("[%d] STATUS => %s (%s)", job.id, hostname,
                             job.actual_device.hostname)
            self.controler.send_multipart([hostname, 'STATUS', str(job.id)])

    def dispatcher_alive(self, hostname):
        if hostname not in self.dispatchers:
            # The server crashed: send a STATUS message
            self.logger.warning("Unknown dispatcher <%s> (server crashed)", hostname)
            self.dispatchers[hostname] = SlaveDispatcher(hostname, online=True)
            self.send_status(hostname)

        # Mark the dispatcher as alive
        self.dispatchers[hostname].alive()

    def logging_socket(self, options):
        self.logger.info("[POLL] Starting logging thread")
        self.pull_socket = self.context.socket(zmq.PULL)
        if options['encrypt']:
            self.pull_socket.curve_publickey = self.master_public
            self.pull_socket.curve_secretkey = self.master_secret
            self.pull_socket.curve_server = True
        self.pull_socket.bind(options['log_socket'])
        jobs = {}
        while self.run_threads:
            try:
                # We need here to use the zmq.NOBLOCK flag, otherwise the thread
                # could be blocked waiting for a message.
                # Trying to stop the dispatcher-master when the pull_socket is
                # empty would then result in this thread to be improperly stopped
                # because it would just never see that the run_threads flag had
                # changed.
                msg = self.pull_socket.recv_multipart(zmq.NOBLOCK)
            except zmq.error.Again:
                msg = None
            if msg is None:
                time.sleep(2) # Don't eat too much CPU
                continue
            try:
                (job_id, level, name, message) = msg  # pylint: disable=unbalanced-tuple-unpacking
            except ValueError:
                # do not let a bad message stop the master.
                self.logger.error("Failed to parse log message, skipping: %s", msg)
                continue

            try:
                scanned = yaml.load(message, Loader=yaml.CLoader)
            except yaml.YAMLError:
                self.logger.error("[%s] data are not valid YAML, dropping", job_id)
                continue

            # Look for "results" level
            try:
                message_lvl = scanned["lvl"]
                message_msg = scanned["msg"]
            except KeyError:
                self.logger.error(
                    "[%s] Invalid log line, missing \"lvl\" or \"msg\" keys: %s",
                    job_id, message)
                continue

            # Clear filename
            if '/' in level or '/' in name:
                self.logger.error("[%s] Wrong level or name received, dropping the message", job_id)
                continue

            # Find the handler (if available)
            if job_id in jobs:
                if level != jobs[job_id].current_level:
                    # Close the old file handler
                    jobs[job_id].sub_log.close()
                    filename = os.path.join(jobs[job_id].output_dir,
                                            "pipeline", level.split('.')[0],
                                            "%s-%s.yaml" % (level, name))
                    mkdir(os.path.dirname(filename))
                    self.current_level = level
                    jobs[job_id].sub_log = open(filename, 'a+')
            else:
                # Query the database for the job
                try:
                    job = TestJob.objects.get(id=job_id)
                except TestJob.DoesNotExist:
                    self.logger.error("[%s] Unknown job id", job_id)
                    continue

                self.logger.info("[%s] Receiving logs from a new job", job_id)
                filename = os.path.join(job.output_dir,
                                        "pipeline", level.split('.')[0],
                                        "%s-%s.yaml" % (level, name))
                # Create the sub directories (if needed)
                mkdir(os.path.dirname(filename))
                jobs[job_id] = JobHandler(job, level, filename)

            if message_lvl == "results":
                try:
                    job = TestJob.objects.get(pk=job_id)
                except TestJob.DoesNotExist:
                    self.logger.error("[%s] Unknown job id", job_id)
                    continue
                meta_filename = create_metadata_store(message_msg, job, level)
                ret = map_scanned_results(results=message_msg, job=job, meta_filename=meta_filename)
                if not ret:
                    self.logger.warning(
                        "[%s] Unable to map scanned results: %s",
                        job_id, message)

            # Mark the file handler as used
            jobs[job_id].last_usage = time.time()

            # n.b. logging here would produce a log entry for every message in every job.
            # The format is a list of dictionaries
            message = "- %s" % message

            # Write data
            jobs[job_id].write(message)
        self.logger.info("[CLOSE] Closing the logging socket and dropping messages")
        self.pull_socket.close(linger=0)
        self.logger.info("[POLL] Terminating logging thread")

    def controler_socket(self):
        msg = self.controler.recv_multipart()
        # This is way to verbose for production and should only be activated
        # by (and for) developers
        # self.logger.debug("[CC] Receiving: %s", msg)

        # 1: the hostname (see ZMQ documentation)
        hostname = msg[0]
        # 2: the action
        action = msg[1]
        # Handle the actions
        if action == 'HELLO' or action == 'HELLO_RETRY':
            self.logger.info("%s => %s", hostname, action)

            # Check the protocol version
            try:
                slave_version = int(msg[2])
            except (IndexError, ValueError):
                self.logger.error("Invalid message from <%s> '%s'", hostname, msg)
                return False
            if slave_version != PROTOCOL_VERSION:
                self.logger.error("<%s> using protocol v%d while master is using v%d",
                                  hostname, slave_version, PROTOCOL_VERSION)
                return False

            self.controler.send_multipart([hostname, 'HELLO_OK'])
            # If the dispatcher is known and sent an HELLO, means that
            # the slave has restarted
            if hostname in self.dispatchers:
                if action == 'HELLO':
                    self.logger.warning("Dispatcher <%s> has RESTARTED",
                                        hostname)
                else:
                    # Assume the HELLO command was received, and the
                    # action succeeded.
                    self.logger.warning("Dispatcher <%s> was not confirmed",
                                        hostname)
            else:
                # No dispatcher, treat HELLO and HELLO_RETRY as a normal HELLO
                # message.
                self.logger.warning("New dispatcher <%s>", hostname)
                self.dispatchers[hostname] = SlaveDispatcher(hostname, online=True)

            if action == 'HELLO':
                # FIXME: slaves need to be allowed to restart cleanly without affecting jobs
                # as well as handling unexpected crashes.
                self._cancel_slave_dispatcher_jobs(hostname)

            # Mark the dispatcher as alive
            self.dispatchers[hostname].alive()

        elif action == 'PING':
            self.logger.debug("%s => PING", hostname)
            # Send back a signal
            self.controler.send_multipart([hostname, 'PONG'])
            self.dispatcher_alive(hostname)

        elif action == 'END':
            try:
                job_id = int(msg[2])
                job_status = int(msg[3])
                error_msg = msg[4]
                description = msg[5]
            except (IndexError, ValueError):
                self.logger.error("Invalid message from <%s> '%s'", hostname, msg)
                return False
            if job_status:
                status = TestJob.INCOMPLETE
                self.logger.info("[%d] %s => END with error %d", job_id, hostname, job_status)
                self.logger.error("[%d] Error: %s", job_id, error_msg)
            else:
                status = TestJob.COMPLETE
                self.logger.info("[%d] %s => END", job_id, hostname)

            # Find the corresponding job and update the status
            try:
                # Save the description
                job = TestJob.objects.get(id=job_id)
                filename = os.path.join(job.output_dir, 'description.yaml')
                try:
                    with open(filename, 'w') as f_description:
                        f_description.write(lzma.decompress(description))
                except (IOError, lzma.error) as exc:
                    self.logger.error("[%d] Unable to dump 'description.yaml'",
                                      job_id)
                    self.logger.exception(exc)
                parse_job_description(job)

                # Update status.
                with transaction.atomic():
                    job = TestJob.objects.select_for_update().get(id=job_id)
                    if job.status == TestJob.CANCELING:
                        cancel_job(job)
                    fail_job(job, fail_msg=error_msg, job_status=status)

            except TestJob.DoesNotExist:
                self.logger.error("[%d] Unknown job", job_id)
            # ACK even if the job is unknown to let the dispatcher
            # forget about it
            self.controler.send_multipart([hostname, 'END_OK', str(job_id)])
            self.dispatcher_alive(hostname)

        elif action == 'START_OK':
            try:
                job_id = int(msg[2])
            except (IndexError, ValueError):
                self.logger.error("Invalid message from <%s> '%s'", hostname, msg)
                return False
            self.logger.info("[%d] %s => START_OK", job_id, hostname)
            try:
                with transaction.atomic():
                    job = TestJob.objects.select_for_update() \
                                         .get(id=job_id)
                    start_job(job)
            except TestJob.DoesNotExist:
                self.logger.error("[%d] Unknown job", job_id)

            self.dispatcher_alive(hostname)

        else:
            self.logger.error("<%s> sent unknown action=%s, args=(%s)",
                              hostname, action, msg[1:])
        return True

    def _cancel_slave_dispatcher_jobs(self, hostname):
        """Get dispatcher jobs and cancel them.

        :param hostname: The name of the dispatcher host.
        :type hostname: string
        """
        # TODO: DB: mark the dispatcher as online in the database.
        # For the moment this should not be done by this process as
        # some dispatchers are using old and new dispatcher.

        # Mark all jobs on this dispatcher as canceled.
        # The dispatcher had (re)started, so all jobs have to be
        # finished.
        with transaction.atomic():
            jobs = TestJob.objects.filter(
                actual_device__worker_host__hostname=hostname,
                is_pipeline=True,
                status=TestJob.RUNNING).select_for_update()

            for job in jobs:
                self.logger.info("[%d] Canceling", job.id)
                cancel_job(job)

    def export_definition(self, job):  # pylint: disable=no-self-use
        job_def = yaml.load(job.definition)
        job_def['compatibility'] = job.pipeline_compatibility

        # no need for the dispatcher to retain comments
        return yaml.dump(job_def)

    def process_jobs(self, options):
        for job in TestJob.objects.filter(
                Q(status=TestJob.SUBMITTED) & Q(is_pipeline=True) & ~Q(actual_device=None))\
                .order_by('-health_check', '-priority', 'submit_time', 'target_group', 'id'):

            device = select_device(job, self.dispatchers)
            if not device:
                continue
            # selecting device can change the job
            job = TestJob.objects.get(id=job.id)
            self.logger.info("[%d] Assigning %s device", job.id, device)
            if job.actual_device is None:
                device = job.requested_device
                if not device.worker_host:
                    msg = "Infrastructure error: Invalid worker information"
                    self.logger.error("[%d] %s", job.id, msg)
                    fail_job(job, msg, TestJob.INCOMPLETE)
                    continue

                # Launch the job
                create_job(job, device)
                self.logger.info("[%d] START => %s (%s)", job.id,
                                 device.worker_host.hostname, device.hostname)
                worker_host = device.worker_host
            else:
                device = job.actual_device
                if not device.worker_host:
                    msg = "Infrastructure error: Invalid worker information"
                    self.logger.error("[%d] %s", job.id, msg)
                    fail_job(job, msg, TestJob.INCOMPLETE)
                    continue
                self.logger.info("[%d] START => %s (%s) (retrying)", job.id,
                                 device.worker_host.hostname, device.hostname)
                worker_host = device.worker_host
            try:
                # Load env.yaml, env-dut.yaml and dispatcher configuration
                # All three are optional
                env_str = load_optional_yaml_file(options['env'])
                env_dut_str = load_optional_yaml_file(options['env_dut'])
                dispatcher_config_file = os.path.join(options['dispatchers_config'],
                                                      "%s.yaml" % worker_host.hostname)
                dispatcher_config = load_optional_yaml_file(dispatcher_config_file)

                if job.is_multinode:
                    for group_job in job.sub_jobs_list:
                        if group_job.dynamic_connection:
                            # to get this far, the rest of the multinode group must also be ready
                            # so start the dynamic connections
                            # FIXME: rationalise and streamline
                            dynamic_connection_worker_host = group_job.lookup_worker
                            self.logger.info("[%d] START => %s (connection)", group_job.id,
                                              dynamic_connection_worker_host.hostname)
                            parent_job = group_job.get_parent_job
                            if parent_job.actual_device is None:
                                parent_device = parent_job.requested_device
                            else:
                                parent_device = parent_job.actual_device

                            job_def = yaml.load(parent_job.definition)
                            job_ctx = job_def.get('context', {})
                            device_configuration = parent_device.load_device_configuration(job_ctx)
                            self.controler.send_multipart(
                                [str(dynamic_connection_worker_host.hostname),
                                 'START', str(group_job.id),
                                 self.export_definition(group_job),
                                 str(device_configuration),
                                 dispatcher_config,
                                 env_str, env_dut_str])

                                # Load job definition to get the variables for template
                # rendering
                job_def = yaml.load(job.definition)
                job_ctx = job_def.get('context', {})

                # Load device configuration
                device_configuration = device.load_device_configuration(job_ctx)

                self.controler.send_multipart(
                    [str(worker_host.hostname),
                     'START', str(job.id),
                     self.export_definition(job),
                     str(device_configuration),
                     dispatcher_config,
                     env_str, env_dut_str])
                continue

            except jinja2.TemplateNotFound as exc:
                self.logger.error("[%d] Template not found: '%s'",
                                  job.id, exc.message)
                msg = "Infrastructure error: Template not found: '%s'" % \
                      exc.message
            except jinja2.TemplateSyntaxError as exc:
                self.logger.error("[%d] Template syntax error in '%s', line %d: %s",
                                  job.id, exc.name, exc.lineno, exc.message)
                msg = "Infrastructure error: Template syntax error in '%s', line %d: %s" % \
                      (exc.name, exc.lineno, exc.message)
            except IOError as exc:
                self.logger.error("[%d] Unable to read '%s': %s",
                                  job.id, exc.filename, exc.strerror)
                msg = "Infrastructure error: cannot open '%s': %s" % \
                      (exc.filename, exc.strerror)
            except yaml.YAMLError as exc:
                self.logger.error("[%d] Unable to parse job definition: %s",
                                  job.id, exc)
                msg = "Infrastructure error: cannot parse job definition: %s" % \
                      exc

            self.logger.error("[%d] INCOMPLETE job", job.id)
            fail_job(job=job, fail_msg=msg, job_status=TestJob.INCOMPLETE)

    def handle_canceling(self):
        for job in TestJob.objects.filter(status=TestJob.CANCELING, is_pipeline=True):
            worker_host = job.lookup_worker if job.dynamic_connection else job.actual_device.worker_host
            if not worker_host:
                self.logger.warning("[%d] Invalid worker information", job.id)
                # shouldn't happen
                fail_job(job, 'invalid worker information', TestJob.CANCELED)
                continue
            self.logger.info("[%d] CANCEL => %s", job.id,
                             worker_host.hostname)
            self.controler.send_multipart([str(worker_host.hostname),
                                           'CANCEL', str(job.id)])

    def logging_support(self):
        del logging.root.handlers[:]
        del logging.root.filters[:]
        # Create the logger
        log_format = '%(asctime)-15s %(levelname)s %(message)s'
        logging.basicConfig(format=log_format, filename='/var/log/lava-server/lava-master.log')
        self.logger = logging.getLogger('dispatcher-master')

    def handle(self, *args, **options):
        # Set a proper umask value
        os.umask(0o022)

        # Set the logging level
        if options['level'] == 'ERROR':
            self.logger.setLevel(logging.ERROR)
        elif options['level'] == 'WARN':
            self.logger.setLevel(logging.WARN)
        elif options['level'] == 'INFO':
            self.logger.setLevel(logging.INFO)
        else:
            self.logger.setLevel(logging.DEBUG)

        auth = None
        # Create the sockets
        self.context = zmq.Context()
        self.controler = self.context.socket(zmq.ROUTER)

        if options['encrypt']:
            self.logger.info("Starting encryption")
            try:
                auth = ThreadAuthenticator(self.context)
                auth.start()
                self.logger.debug("Opening master certificate: %s", options['master_cert'])
                self.master_public, self.master_secret = zmq.auth.load_certificate(options['master_cert'])
                self.logger.debug("Using slaves certificates from: %s", options['slaves_certs'])
                auth.configure_curve(domain='*', location=options['slaves_certs'])
            except IOError as err:
                self.logger.error(err)
                auth.stop()
                return
            self.controler.curve_publickey = self.master_public
            self.controler.curve_secretkey = self.master_secret
            self.controler.curve_server = True

        self.controler.bind(options['master_socket'])

        # Last access to the database for new jobs and cancelations
        last_db_access = 0

        # Poll on the sockets (only one for the moment). This allow to have a
        # nice timeout along with polling.
        poller = zmq.Poller()
        poller.register(self.controler, zmq.POLLIN)

        # Mask signals and create a pipe that will receive a bit for each
        # signal received. Poll the pipe along with the zmq socket so that we
        # can only be interupted while reading data.
        (pipe_r, pipe_w) = os.pipe()
        flags = fcntl.fcntl(pipe_w, fcntl.F_GETFL, 0)
        fcntl.fcntl(pipe_w, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        def signal_to_pipe(signumber, _):
            # Send the signal number on the pipe
            os.write(pipe_w, chr(signumber))

        signal.signal(signal.SIGHUP, signal_to_pipe)
        signal.signal(signal.SIGINT, signal_to_pipe)
        signal.signal(signal.SIGTERM, signal_to_pipe)
        signal.signal(signal.SIGQUIT, signal_to_pipe)
        poller.register(pipe_r, zmq.POLLIN)

        if os.path.exists('/etc/lava-server/worker.conf'):
            self.logger.error("[FAIL] lava-master must not be run on a remote worker!")
            self.controler.close(linger=0)
            self.context.term()
            sys.exit(2)

        self.logger.info("[INIT] LAVA dispatcher-master has started.")
        self.logger.info("[INIT] Using protocol version %d", PROTOCOL_VERSION)

        self.run_threads = True

        # Logging socket
        logging_thread = Thread(target=self.logging_socket, args=(options, ))
        logging_thread.start()

        while True:
            try:
                try:
                    # TODO: Fix the timeout computation
                    # Wait for data or a timeout
                    sockets = dict(poller.poll(TIMEOUT * 1000))
                except zmq.error.ZMQError:
                    continue

                if sockets.get(pipe_r) == zmq.POLLIN:
                    signum = ord(os.read(pipe_r, 1))
                    if signum == signal.SIGHUP:
                        self.logger.info("[POLL] SIGHUP received, restarting loggers")
                        self.logging_support()
                    else:
                        self.logger.info("[POLL] Received a signal, leaving")
                        break

                # Garbage collect file handlers
                now = time.time()
                for job_id in self.jobs.keys():  # pylint: disable=consider-iterating-dictionary
                    if now - self.jobs[job_id].last_usage > FD_TIMEOUT:
                        self.logger.info("[%s] Closing log file", job_id)
                        self.jobs[job_id].close()
                        del self.jobs[job_id]

                # Command socket
                if sockets.get(self.controler) == zmq.POLLIN:
                    if not self.controler_socket():
                        continue

                # Check dispatchers status
                now = time.time()
                for hostname, dispatcher in self.dispatchers.iteritems():
                    if dispatcher.online and now - dispatcher.last_msg > DISPATCHER_TIMEOUT:
                        self.logger.error("[STATE] Dispatcher <%s> goes OFFLINE", hostname)
                        self.dispatchers[hostname].online = False
                        # TODO: DB: mark the dispatcher as offline and attached
                        # devices

                # Limit accesses to the database. This will also limit the rate of
                # CANCEL and START messages
                if now - last_db_access > DB_LIMIT:
                    last_db_access = now

                    # TODO: make this atomic
                    # Dispatch pipeline jobs with devices in Reserved state
                    self.process_jobs(options)

                    # Handle canceling jobs
                    self.handle_canceling()
            except (OperationalError, InterfaceError):
                self.logger.info("[RESET] database connection reset.")
                continue

        # Closing sockets and droping messages.
        self.logger.info("[CLOSE] Closing the sockets and dropping messages")
        self.controler.close(linger=0)
        self.run_threads = False
        logging_thread.join()
        if options['encrypt']:
            auth.stop()
        self.context.term()
