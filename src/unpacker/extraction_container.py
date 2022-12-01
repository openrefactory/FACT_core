from __future__ import annotations

import logging
from contextlib import suppress
from os import getgid, getuid
from tempfile import TemporaryDirectory

import docker
from docker.errors import APIError, DockerException
from docker.models.containers import Container
from docker.types import Mount

DOCKER_CLIENT = docker.from_env()
EXTRACTOR_DOCKER_IMAGE = 'extractor_uaas_test:latest'


class ExtractionContainer:
    def __init__(self, config, id_: int):
        self.config = config
        self.id_ = id_

        self.tmp_dir = TemporaryDirectory(  # pylint: disable=consider-using-with
            dir=config['data-storage']['docker-mount-base-dir']
        )
        self.port = self.config.getint('unpack', 'base-port') + id_
        self.memory_limit = config['unpack']['memory-limit']

        self.container = None
        self.exception = False

    def start(self):
        if self.container is not None:
            raise RuntimeError('Already running.')

        try:
            self._start_container()
        except APIError as exception:
            if 'port is already allocated' in str(exception):
                self._recover_from_port_in_use(exception)

    def _recover_from_port_in_use(self, exception: Exception):
        logging.warning('Extractor port already in use -> trying to remove old container...')
        for running_container in DOCKER_CLIENT.containers.list():
            if self._is_extractor_container(running_container) and self._has_same_port(running_container):
                self._remove_container(running_container)
                self._start_container()
                return
        logging.error('Could not free extractor port')
        raise RuntimeError('Could not create extractor container') from exception

    def _start_container(self):
        volume = Mount('/tmp/extractor', self.tmp_dir.name, read_only=False, type='bind')
        self.container = DOCKER_CLIENT.containers.run(
            image=EXTRACTOR_DOCKER_IMAGE,
            ports={'5000/tcp': self.port},
            mem_limit=self.memory_limit,
            mounts=[volume],
            volumes={'/dev': {'bind': '/dev', 'mode': 'rw'}},
            privileged=True,
            detach=True,
            remove=True,
            environment={'CHMOD_OWNER': f'{getuid()}:{getgid()}'},
            entrypoint='gunicorn -w 1 -b 0.0.0.0:5000 server:app',
        )
        logging.info(f'Started unpack worker {self.id_}')

    @staticmethod
    def _is_extractor_container(container: Container):
        return any(tag == EXTRACTOR_DOCKER_IMAGE for tag in container.image.attrs['RepoTags'])

    def _has_same_port(self, container: Container):
        return any(entry['HostPort'] == str(self.port) for entry in container.ports.get('5000/tcp', []))

    def stop(self):
        if self.container is None:
            raise RuntimeError('Container is not running.')

        logging.info(f'Stopping unpack worker {self.id_}')
        self._remove_container(self.container)
        try:
            self.tmp_dir.cleanup()
        except PermissionError:
            logging.exception(f'Unable to delete worker folder {self.tmp_dir.name}')

    def exception_happened(self):
        return self.exception

    @staticmethod
    def _remove_container(container: Container):
        container.stop(timeout=5)
        with suppress(DockerException):
            container.remove()

    def restart(self):
        self.stop()
        self.tmp_dir = TemporaryDirectory(  # pylint: disable=consider-using-with
            dir=self.config['data-storage']['docker-mount-base-dir']
        )
        self.exception = False
        self.container = None
        self.start()
