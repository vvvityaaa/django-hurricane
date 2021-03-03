import socket
import subprocess
from queue import Empty, Queue
from threading import Thread
from time import sleep
from typing import List, Tuple

import docker

from hurricane.testing.actors import TestPublisher


class HurricaneBaseDriver(object):
    proc = None
    log_lines = []
    base_command = []
    coverage_base_command = []
    test_string = ""
    ports = [8000, 8001]

    def __init__(self):
        for port in self.ports:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    raise Exception(f"Port {port} already in use.")

    def get_server_host_port(self, probe_port=False) -> Tuple[str, int]:
        if probe_port:
            port = self.probe_port
        else:
            port = self.port
        if self.proc:
            return "localhost", port
        else:
            return None, None

    def get_output(self, read_all=False) -> Tuple[str, str]:
        if self.proc:
            if read_all:
                while True:
                    try:
                        line = self.q.get(timeout=0.5)
                        self.log_lines.append(line)
                    except Empty:
                        break
                return "".join(self.log_lines), ""
            else:
                try:
                    line = self.q.get(timeout=1)
                    if line:
                        return line, ""
                except Empty:
                    pass
                return "", ""
        return "", ""

    def _start(self, params: List[str] = None, coverage: bool = True) -> None:
        self.log_lines = []
        if coverage:
            base_command = self.coverage_base_command
        else:
            base_command = self.base_command

        def enqueue_stdout(proc, queue):
            out = proc.stdout
            for line in iter(out.readline, b""):
                queue.put(line.decode("utf-8"))
            out.close()

        def enqueue_stderr(proc, queue):
            out = proc.stderr
            for line in iter(out.readline, b""):
                queue.put(line.decode("utf-8"))
            out.close()

        if params:
            base_command = base_command + params
        if params and "--port" in params:
            self.port = int(params[params.index("--port") + 1])
        else:
            self.port = 8000

        if params and "--probe-port" in params:
            self.probe_port = int(params[params.index("--probe-port") + 1])
        else:
            self.probe_port = 8001

        self.proc = subprocess.Popen(base_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.q = Queue()
        self.t_stderr = Thread(target=enqueue_stderr, args=(self.proc, self.q))
        self.t_stdout = Thread(target=enqueue_stdout, args=(self.proc, self.q))
        self.t_stderr.daemon = True
        self.t_stderr.start()
        self.t_stdout.daemon = True
        self.t_stdout.start()
        # wait a maximum of 1 second
        for i in range(0, 10):
            out, err = self.get_output(read_all=True)
            if self.test_string in out:
                break
            sleep(0.1)

    def _stop(self):
        if self.proc:
            self.proc.terminate()


class HurricaneServerDriver(HurricaneBaseDriver):
    coverage_base_command = [
        "coverage",
        "run",
        "--source=hurricane/",
        "manage.py",
        "serve",
    ]
    base_command = ["python", "manage.py", "serve"]
    test_string = "Starting a Tornado-powered Django web server"

    def start_server(self, params: dict = None, coverage: bool = True) -> None:
        self._start(params, coverage)

    def stop_server(self) -> None:
        self._stop()


class HurricaneWebhookServerDriver(HurricaneBaseDriver):
    coverage_base_command = [
        "coverage",
        "run",
        "--source=hurricane/",
        "start_receiver.py",
    ]
    # base_command = ["python", "manage.py", "webhook_test_command"]
    base_command = ["python", "start_receiver.py"]
    test_string = "Started webhook server"

    def start_server(self, params: dict = None, coverage: bool = True) -> None:
        self._start(params, coverage)

    def stop_server(self) -> None:
        self._stop()


class HurricaneAMQPDriver(HurricaneBaseDriver):
    coverage_base_command = [
        "coverage",
        "run",
        "--source=hurricane/",
        "manage.py",
        "consume",
    ]
    base_command = ["python", "manage.py", "consume"]
    test_string = "Starting a Tornado-powered Django AMQP consumer"
    ports = [5672, 8000, 8001]

    def start_amqp(self) -> None:
        client = docker.from_env()
        if hasattr(self, "container") and self.container:
            c = client.containers.run(
                "quay.io/blueshoe/rabbitmq3.8-alpine",
                auto_remove=True,
                detach=True,
                ports={"5672": ("127.0.0.1", self._temp_port)},
            )
        else:
            c = client.containers.run(
                "quay.io/blueshoe/rabbitmq3.8-alpine",
                auto_remove=True,
                detach=True,
                ports={"5672": ("127.0.0.1", None)},
            )
        self.container = client.containers.get(c.id)
        # busy wait for rabbitmq to come up (timeout 10 seconds)
        for i in range(0, 20):
            if "Ready to start client connection listeners" in self.container.logs().decode("utf-8"):
                break
            else:
                sleep(0.5)

    def get_test_publisher(self, vhost="/"):
        host, port = self.get_amqp_host_port()
        return TestPublisher(host, port, vhost)

    def start_consumer(self, params: List[str] = None, coverage: bool = True) -> None:
        self._start(params, coverage)

    def stop_amqp(self) -> None:
        if hasattr(self, "container") and self.container:
            try:
                self.container.kill()
                delattr(self, "container")
            except Exception:
                # this container is potentially already stopped
                delattr(self, "container")

    def halt_amqp(self) -> None:
        if hasattr(self, "container") and self.container:
            try:
                _, self._temp_port = self.get_amqp_host_port()
                self.container.kill()
            except Exception:
                # this container is potentially already stopped
                pass

    def stop_consumer(self) -> None:
        self._stop()

    def _get_port(self):
        if hasattr(self, "container") and self.container:
            self._temp_port = self.container.attrs["NetworkSettings"]["Ports"]["5672/tcp"][0]["HostPort"]
            return self._temp_port
        return None

    def get_amqp_host_port(self) -> Tuple[str, int]:
        if port := self._get_port():
            return "127.0.0.1", port
        else:
            return None, None
