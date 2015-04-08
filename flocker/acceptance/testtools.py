# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
Testing utilities for ``flocker.acceptance``.
"""
from json import dumps
from os import environ
from pipes import quote as shell_quote
from socket import gaierror, socket
from subprocess import check_call, PIPE, Popen
from unittest import SkipTest, skipUnless
from yaml import safe_dump

from twisted.web.http import OK, CREATED
from twisted.internet.defer import succeed
from twisted.python.filepath import FilePath
from twisted.python.procutils import which

from treq import get, post, delete, json_content
from pyrsistent import PRecord, field, CheckedPVector, pmap

from ..control import (
    Application, AttachedVolume, DockerImage, Manifestation, Dataset,
)
from ..control.httpapi import container_configuration_response, REST_API_PORT

from flocker.testtools import loop_until


try:
    from pymongo import MongoClient
    from pymongo.errors import ConnectionFailure
    PYMONGO_INSTALLED = True
except ImportError:
    PYMONGO_INSTALLED = False

__all__ = [
    'assert_expected_deployment', 'flocker_deploy', 'get_nodes',
    'MONGO_APPLICATION', 'MONGO_IMAGE', 'get_mongo_application',
    'require_flocker_cli', 'get_node_state', 'create_application',
    'create_attached_volume'
    ]

# XXX This assumes that the desired version of flocker-cli has been installed.
# Instead, the testing environment should do this automatically.
# See https://clusterhq.atlassian.net/browse/FLOC-901.
require_flocker_cli = skipUnless(which("flocker-deploy"),
                                 "flocker-deploy not installed")

require_mongo = skipUnless(
    PYMONGO_INSTALLED, "PyMongo not installed")


# XXX The MONGO_APPLICATION will have to be removed because it does not match
# the tutorial yml files, and the yml should be testably the same:
# https://clusterhq.atlassian.net/browse/FLOC-947
MONGO_APPLICATION = u"mongodb-example-application"
MONGO_IMAGE = u"clusterhq/mongodb"


def get_mongo_application():
    """
    Return a new ``Application`` with a name and image corresponding to
    the MongoDB tutorial example:

    http://doc-dev.clusterhq.com/gettingstarted/tutorial/index.html
    """
    return Application(
        name=MONGO_APPLICATION,
        image=DockerImage.from_string(MONGO_IMAGE + u':latest'),
    )


def create_application(name, image, ports=frozenset(), volume=None,
                       links=frozenset(), environment=None, memory_limit=None,
                       cpu_shares=None):
    """
    Instantiate an ``Application`` with the supplied parameters and return it.
    """
    return Application(
        name=name, image=DockerImage.from_string(image + u':latest'),
        ports=ports, volume=volume, links=links, environment=environment,
        memory_limit=memory_limit, cpu_shares=cpu_shares
    )


def create_attached_volume(dataset_id, mountpoint, maximum_size=None,
                           metadata=pmap()):
    """
    Create an ``AttachedVolume`` instance with the supplied parameters and
    return it.

    :param unicode dataset_id: The unique identifier of the dataset of the
        attached volume.
    :param bytes mountpoint: The path at which the volume is attached.
    :param int maximum_size: An optional maximum size for the volume.

    :return: A new ``AttachedVolume`` instance referencing a primary
        manifestation of a dataset with the given unique identifier.
    """
    return AttachedVolume(
        manifestation=Manifestation(
            dataset=Dataset(
                dataset_id=dataset_id,
                maximum_size=maximum_size,
                metadata=metadata,
            ),
            primary=True,
        ),
        mountpoint=FilePath(mountpoint),
    )


def get_node_state(cluster, hostname):
    """
    Get the applications on a node using the HTTP API.

    :param Cluster cluster: The cluster to talk to.
    :param hostname: The hostname of the node.

    :return: ``Deferred`` that fires with a tuple of the ``Cluster`` and a
        ``list`` of ``Application`` currently on that node.
    """
    d = cluster.current_containers()
    d.addCallback(
        lambda result: (cluster, {app.name: app for app in result[1]
                                  if app[u"hostname"] == hostname}))
    return d


def run_SSH(port, user, node, command, input, key=None,
            background=False):
    """
    Run a command via SSH.

    :param int port: Port to connect to.
    :param bytes user: User to run the command as.
    :param bytes node: Node to run command on.
    :param command: Command to run.
    :type command: ``list`` of ``bytes``.
    :param bytes input: Input to send to command.
    :param FilePath key: If not None, the path to a private key to use.
    :param background: If ``True``, don't block waiting for SSH process to
         end or read its stdout. I.e. it will run "in the background".
         Also ensures remote process has pseudo-tty so killing the local SSH
         process will kill the remote one.

    :return: stdout as ``bytes`` if ``background`` is false, otherwise
        return the ``subprocess.Process`` object.
    """
    quotedCommand = ' '.join(map(shell_quote, command))
    command = [
        b'ssh',
        b'-p', b'%d' % (port,),
        ]

    if key is not None:
        command.extend([
            b"-i",
            key.path])

    if background:
        # Force pseudo-tty so that remote process exists when the ssh
        # client does:
        command.extend([b"-t", b"-t"])

    command.extend([
        b'@'.join([user, node]),
        quotedCommand
    ])
    if background:
        process = Popen(command, stdin=PIPE)
        process.stdin.write(input)
        return process
    else:
        process = Popen(command, stdout=PIPE, stdin=PIPE)

    result = process.communicate(input)
    if process.returncode != 0:
        raise Exception('Command Failed', command, process.returncode)

    return result[0]


def _clean_node(test_case, node):
    """
    Remove all containers and zfs volumes on a node, given the IP address of
    the node.

    :param test_case: The ``TestCase`` running this unit test.
    :param bytes node: The hostname or IP of the node.
    """
    # Without the below, deploying the same application with a data volume
    # twice fails. See the error given with the tutorial's yml files:
    #
    #   $ flocker-deploy volume-deployment.yml volume-application.yml
    #   $ ssh root@${NODE} docker ps -a -q # outputs an ID, ${ID}
    #   $ ssh root@${NODE} docker stop ${ID}
    #   $ ssh root@${NODE} docker rm ${ID}
    #   $ flocker-deploy volume-deployment.yml volume-application.yml
    #
    # http://doc-dev.clusterhq.com/advanced/cleanup.html#removing-zfs-volumes
    # A tool or flocker-deploy option to purge the state of a node does
    # not yet exist. See https://clusterhq.atlassian.net/browse/FLOC-682
    run_SSH(22, 'root', node, [b"zfs"] + [b"destroy"] + [b"-r"] +
            [b"flocker"], None)


def get_nodes(test_case, num_nodes):
    """
    Create or get ``num_nodes`` nodes with no Docker containers on them.

    This is an alternative to
    http://doc-dev.clusterhq.com/gettingstarted/tutorial/
    vagrant-setup.html#creating-vagrant-vms-needed-for-flocker

    XXX This pretends to be asynchronous because num_nodes Docker containers
    will be created instead to replace this in some circumstances, see
    https://clusterhq.atlassian.net/browse/FLOC-900

    :param test_case: The ``TestCase`` running this unit test.
    :param int num_nodes: The number of nodes to start up.

    :return: A ``Deferred`` which fires with a set of IP addresses.
    """

    nodes_env_var = environ.get("FLOCKER_ACCEPTANCE_NODES")

    if nodes_env_var is None:
        raise SkipTest(
            "Set acceptance testing node IP addresses using the " +
            "FLOCKER_ACCEPTANCE_NODES environment variable and a colon " +
            "separated list.")

    # Remove any empty strings, for example if the list has ended with a colon
    nodes = filter(None, nodes_env_var.split(':'))

    if len(nodes) < num_nodes:
        raise SkipTest("This test requires a minimum of {necessary} nodes, "
                       "{existing} node(s) are set.".format(
                           necessary=num_nodes, existing=len(nodes)))

    reachable_nodes = set()

    for node in nodes:
        sock = socket()
        try:
            can_connect = not sock.connect_ex((node, 22))
        except gaierror:
            can_connect = False
        finally:
            if can_connect:
                reachable_nodes.add(node)
            sock.close()

    if len(reachable_nodes) < num_nodes:
        unreachable_nodes = set(nodes) - reachable_nodes
        test_case.fail(
            "At least {min} node(s) must be running and reachable on port 22. "
            "The following node(s) are reachable: {reachable}. "
            "The following node(s) are not reachable: {unreachable}.".format(
                min=num_nodes,
                reachable=", ".join(str(node) for node in reachable_nodes),
                unreachable=", ".join(str(node) for node in unreachable_nodes),
            )
        )

    # Only return the desired number of nodes
    reachable_nodes = set(sorted(reachable_nodes)[:num_nodes])

    # Remove all existing containers:
    clean_deploy = {u"version": 1,
                    u"nodes": {}}
    clean_applications = {u"version": 1,
                          u"applications": {}}
    flocker_deploy(test_case, clean_deploy, clean_applications)
    getting = get_test_cluster()

    def no_containers(cluster):
        d = cluster.current_containers()
        d.addCallback(lambda result: len(result[1]) == 0)
        return d
    getting.addCallback(lambda cluster:
                        loop_until(lambda: no_containers(cluster)))

    def clean_zfs(_):
        for node in reachable_nodes:
            _clean_node(test_case, node)
    getting.addCallback(clean_zfs)
    getting.addCallback(lambda _: reachable_nodes)
    return getting


def flocker_deploy(test_case, deployment_config, application_config):
    """
    Run ``flocker-deploy`` with given configuration files.

    :param test_case: The ``TestCase`` running this unit test.
    :param dict deployment_config: The desired deployment configuration.
    :param dict application_config: The desired application configuration.
    """
    control_node = environ.get("FLOCKER_ACCEPTANCE_CONTROL_NODE")
    if control_node is None:
        raise SkipTest("Set control node address using "
                       "FLOCKER_ACCEPTANCE_CONTROL_NODE environment variable.")

    temp = FilePath(test_case.mktemp())
    temp.makedirs()

    deployment = temp.child(b"deployment.yml")
    deployment.setContent(safe_dump(deployment_config))

    application = temp.child(b"application.yml")
    application.setContent(safe_dump(application_config))

    check_call([b"flocker-deploy", control_node, deployment.path,
                application.path])


def get_mongo_client(host, port=27017):
    """
    Returns a ``Deferred`` which fires with a ``MongoClient`` when one has been
    created.

    See http://api.mongodb.org/python/current/api/pymongo/mongo_client.html#
        pymongo.mongo_client.MongoClient
    for more parameter information.

    :param bytes host: Hostname or IP address of the instance to connect to.
    :param int port: Port number on which to connect.

    The tutorial says "If you get a connection refused error try again after a
    few seconds; the application might take some time to fully start up."
    and so here we wait until the client can be created.
    """
    def create_mongo_client():
        try:
            return MongoClient(host=host, port=port)
        except ConnectionFailure:
            return False

    d = loop_until(create_mongo_client)
    return d


def assert_expected_deployment(test_case, expected_deployment):
    """
    Assert that the expected set of ``Application`` instances on a set of
    nodes is the same as the actual set of ``Application`` instance on
    those nodes.

    The tutorial looks at Docker output, but the acceptance tests are
    intended to test high-level external behaviors. Since this is looking
    at the output of the control service API it merely verifies what
    Flocker believes the system state is, not the actual state.
    The latter should be verified separately with additional tests
    for external side-effects (applications being available on ports,
    say).

    :param test_case: The ``TestCase`` running this unit test.
    :param dict expected_deployment: A mapping of IP addresses to set of
        ``Application`` instances expected on the nodes with those IP
        addresses.

    :return Deferred: Fires on end of assertion.
    """
    d = get_test_cluster()

    def got_cluster(cluster):
        def got_results(results):
            cluster, existing_containers = results
            expected = []
            for hostname, apps in expected_deployment.items():
                expected += [container_configuration_response(app, hostname)
                             for app in apps]
            for app in expected:
                app[u"running"] = True
            return sorted(existing_containers) == sorted(expected)

        def configuration_matches_state():
            d = cluster.current_containers()
            d.addCallback(got_results)
            return d

        return loop_until(configuration_matches_state)
    d.addCallback(got_cluster)
    return d


class Node(PRecord):
    """
    A record of a cluster node.

    :ivar bytes address: The IPv4 address of the node.
    """
    address = field(type=bytes)


class _NodeList(CheckedPVector):
    """
    A list of nodes.

    See https://github.com/tobgu/pyrsistent/issues/26 for more succinct
    idiom combining this with ``field()``.
    """
    __type__ = Node


def check_and_decode_json(result, response_code):
    """
    Given ``treq`` response object, extract JSON and ensure response code
    is the expected one.

    :param result: ``treq`` response.
    :param int response_code: Expected response code.

    :return: ``Deferred`` firing with decoded JSON.
    """
    if result.code != response_code:
        raise ValueError("Unexpected response code:", result.code)
    return json_content(result)


class Cluster(PRecord):
    """
    A record of the control service and the nodes in a cluster for acceptance
    testing.

    :param Node control_node: The node running the ``flocker-control``
        service.
    :param list nodes: The ``Node`` s in this cluster.
    """
    control_node = field(type=Node)
    nodes = field(type=_NodeList)

    @property
    def base_url(self):
        """
        :returns: The base url for API requests to this cluster's control
            service.
        """
        return b"http://{}:{}/v1".format(
            self.control_node.address, REST_API_PORT
        )

    def datasets_state(self):
        """
        Return the actual dataset state of the cluster.

        :return: ``Deferred`` firing with a list of dataset dictionaries,
            the state of the cluster.
        """
        request = get(self.base_url + b"/state/datasets", persistent=False)
        request.addCallback(check_and_decode_json, OK)
        return request

    def wait_for_dataset(self, dataset_properties):
        """
        Poll the dataset state API until the supplied dataset exists.

        :param dict dataset_properties: The attributes of the dataset that
            we're waiting for.
        :returns: A ``Deferred`` which fires with a 2-tuple of ``Cluster`` and
            API response when a dataset with the supplied properties appears in
            the cluster.
        """
        def created():
            """
            Check the dataset state list for the expected dataset.
            """
            request = self.datasets_state()

            def got_body(body):
                # State listing doesn't have metadata or deleted, but does
                # have unpredictable path.
                expected_dataset = dataset_properties.copy()
                del expected_dataset[u"metadata"]
                del expected_dataset[u"deleted"]
                for dataset in body:
                    dataset.pop("path")
                return expected_dataset in body
            request.addCallback(got_body)
            return request

        waiting = loop_until(created)
        waiting.addCallback(lambda ignored: (self, dataset_properties))
        return waiting

    def create_dataset(self, dataset_properties):
        """
        Create a dataset with the supplied ``dataset_properties``.

        :param dict dataset_properties: The properties of the dataset to
            create.
        :returns: A ``Deferred`` which fires with a 2-tuple of ``Cluster`` and
            API response when a dataset with the supplied properties has been
            persisted to the cluster configuration.
        """
        request = post(
            self.base_url + b"/configuration/datasets",
            data=dumps(dataset_properties),
            headers={b"content-type": b"application/json"},
            persistent=False
        )

        request.addCallback(check_and_decode_json, CREATED)
        # Return cluster and API response
        request.addCallback(lambda response: (self, response))
        return request

    def update_dataset(self, dataset_id, dataset_properties):
        """
        Update a dataset with the supplied ``dataset_properties``.

        :param unicode dataset_id: The uuid of the dataset to be modified.
        :param dict dataset_properties: The properties of the dataset to
            create.
        :returns: A 2-tuple of (cluster, api_response)
        """
        request = post(
            self.base_url + b"/configuration/datasets/%s" % (
                dataset_id.encode('ascii'),
            ),
            data=dumps(dataset_properties),
            headers={b"content-type": b"application/json"},
            persistent=False
        )

        request.addCallback(check_and_decode_json, OK)
        # Return cluster and API response
        request.addCallback(lambda response: (self, response))
        return request

    def delete_dataset(self, dataset_id):
        """
        Delete a dataset.

        :param unicode dataset_id: The uuid of the dataset to be modified.

        :returns: A 2-tuple of (cluster, api_response)
        """
        request = delete(
            self.base_url + b"/configuration/datasets/%s" % (
                dataset_id.encode('ascii'),
            ),
            headers={b"content-type": b"application/json"},
            persistent=False
        )

        request.addCallback(check_and_decode_json, OK)
        # Return cluster and API response
        request.addCallback(lambda response: (self, response))
        return request

    def create_container(self, properties):
        """
        Create a container with the specified properties.

        :param dict properties: A ``dict`` mapping to the API request fields
            to create a container.

        :returns: A tuple of (cluster, api_response)
        """
        request = post(
            self.base_url + b"/configuration/containers",
            data=dumps(properties),
            headers={b"content-type": b"application/json"},
            persistent=False
        )

        request.addCallback(check_and_decode_json, CREATED)
        request.addCallback(lambda response: (self, response))
        return request

    def remove_container(self, name):
        """
        Remove a container.

        :param unicode name: The name of the container to remove.

        :returns: A tuple of (cluster, api_response)
        """
        request = delete(
            self.base_url + b"/configuration/containers/" +
            name.encode("ascii"),
            persistent=False
        )

        request.addCallback(check_and_decode_json, OK)
        request.addCallback(lambda response: (self, response))
        return request

    def current_containers(self):
        """
        Get current containers.

        :return: A ``Deferred`` firing with a tuple (cluster instance, API
            response).
        """
        request = get(
            self.base_url + b"/state/containers",
            persistent=False
        )

        request.addCallback(check_and_decode_json, OK)
        request.addCallback(lambda response: (self, response))
        return request


def get_test_cluster(node_count=0):
    """
    Build a ``Cluster`` instance with at least ``node_count`` nodes.

    :param int node_count: The number of nodes to ensure in the cluster.

    :returns: A ``Deferred`` which fires with a ``Cluster`` instance.
    """
    control_node = environ.get('FLOCKER_ACCEPTANCE_CONTROL_NODE')

    if control_node is None:
        raise SkipTest(
            "Set acceptance testing control node IP address using the " +
            "FLOCKER_ACCEPTANCE_CONTROL_NODE environment variable.")

    agent_nodes_env_var = environ.get('FLOCKER_ACCEPTANCE_AGENT_NODES')

    if agent_nodes_env_var is None:
        raise SkipTest(
            "Set acceptance testing node IP addresses using the " +
            "FLOCKER_ACCEPTANCE_AGENT_NODES environment variable and a " +
            "colon separated list.")

    agent_nodes = filter(None, agent_nodes_env_var.split(':'))

    if len(agent_nodes) < node_count:
        raise SkipTest("This test requires a minimum of {necessary} nodes, "
                       "{existing} node(s) are set.".format(
                           necessary=node_count, existing=len(agent_nodes)))

    return succeed(Cluster(
        control_node=Node(address=control_node),
        nodes=map(lambda address: Node(address=address), agent_nodes),
    ))
