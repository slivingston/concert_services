#!/usr/bin/env python
#
# License: BSD
#   https://raw.github.com/robotics-in-concert/concert_services/license/LICENSE
#
##############################################################################
# About
##############################################################################

# Simple script to manage spawning and killing of turtles across multimaster
# boundaries. Typically turtlesim clients would connect to the kill and
# spawn services directly to instantiate themselves, but since we can't
# flip service proxies, this is not possible. So this node is the inbetween
# go-to node and uses a rocon service pair instead.
#
# It supplements this relay role with a bit of herd management - sets up
# random start locations and feeds back aliased names when running with
# a concert.

##############################################################################
# Imports
##############################################################################

import copy
import math
import os
import random
import tempfile

import concert_service_utilities
import gateway_msgs.msg as gateway_msgs
import gateway_msgs.srv as gateway_srvs
import rocon_launch
import rospy
import rocon_gateway_utils
import rocon_python_comms
import std_msgs.msg as std_msgs
import turtlesim.srv as turtlesim_srvs

##############################################################################
# Utilities
##############################################################################


def prepare_launch_configurations(turtles):
    port = 11
    launch_text = '<concert>\n'
    for name in turtles:
        launch_text += '  <launch title="%s:114%s" package="concert_service_turtlesim" name="turtle2.launch" port="114%s">\n' % (name, str(port), str(port))
        launch_text += '    <arg name="turtle_name" value="%s"/>\n' % name
        launch_text += '    <arg name="turtle_concert_whitelist" value="Turtle Concert;Turtle Teleop Concert;Concert Tutorial"/>\n'
        launch_text += '    <arg name="turtle_rapp_whitelist" value="[rocon_apps, turtle_concert]"/>\n'
        launch_text += '  </launch>\n'
        port = port + 1
    launch_text += '</concert>\n'
    temp = tempfile.NamedTemporaryFile(mode='w+t', delete=False)
    #print("\n" + console.green + rocon_launch_text + console.reset)
    temp.write(launch_text)
    rospy.logwarn("Turtle Herder: rocon launch text\n%s" % launch_text)
    temp.close()  # unlink it later
    launch_configurations = rocon_launch.parse_rocon_launcher(temp.name, "--screen")
    try:
        os.unlink(temp.name)
    except OSError:
        rospy.logerr("Turtle Herder : failed to unlink the rocon launcher.")
    return launch_configurations

##############################################################################
# Turtle Herder
##############################################################################


class TurtleHerder:
    '''
      Shepherds the turtles!

      @todo get alised names from the concert client list if the topic is available

      @todo watchdog for killing turtles that are no longer connected.
    '''
    __slots__ = [
        'turtles',              # list of turtle name strings
        '_kill_turtle_service_client',
        '_spawn_turtle_service_client',
        '_gateway_flip_service',
        '_processes',
        '_temporary_files',     # temporary files that have to be unlinked later
        'is_disabled',          # flag set when service manager tells it to shut down.
        '_terminal',  # terminal to use to spawn concert clients
    ]

    def __init__(self):
        self.turtles = []
        self._processes = []
        self._temporary_files = []
        self.is_disabled = False
        # herding backend
        rospy.wait_for_service('kill')  # could use timeouts here
        rospy.wait_for_service('spawn')
        self._kill_turtle_service_client = rospy.ServiceProxy('kill', turtlesim_srvs.Kill, persistent=True)
        self._spawn_turtle_service_client = rospy.ServiceProxy('spawn', turtlesim_srvs.Spawn, persistent=True)
        # kill the default turtle that turtlesim starts with
        try:
            unused_response = self._kill_turtle_service_client("turtle1")
        except rospy.ServiceException:
            rospy.logerr("Turtle Herder : failed to contact the internal kill turtle service")
        except rospy.ROSInterruptException:
            rospy.loginfo("Turtle Herder : shutdown while contacting the internal kill turtle service")
            return
        self._shutdown_subscriber = rospy.Subscriber('shutdown', std_msgs.Empty, self.shutdown)
        # gateway
        gateway_namespace = rocon_gateway_utils.resolve_local_gateway()
        rospy.wait_for_service(gateway_namespace + '/flip')
        self._gateway_flip_service = rospy.ServiceProxy(gateway_namespace + '/flip', gateway_srvs.Remote)
        # set up a terminal type for spawning
        try:
            self._terminal = rocon_launch.create_terminal()
        except (rocon_launch.UnsupportedTerminal, rocon_python_comms.NotFoundException) as e:
            rospy.logwarn("Turtle Herder : cannot find a suitable terminal, falling back to spawning inside the current one [%s]" % str(e))
            self._terminal = rocon_launch.create_terminal(rocon_launch.terminals.active)

    def _spawn_simulated_turtles(self, turtles):
        """
        Very important to have checked that the turtle names are unique
        before calling this method.

        :param turtles str[]: names of the turtles to spawn.
        """
        for turtle in turtles:
            internal_service_request = turtlesim_srvs.SpawnRequest(
                                                random.uniform(3.5, 6.5),
                                                random.uniform(3.5, 6.5),
                                                random.uniform(0.0, 2.0 * math.pi),
                                                turtle)
            try:
                unused_internal_service_response = self._spawn_turtle_service_client(internal_service_request)
                self.turtles.append(turtle)
            except rospy.ServiceException:  # communication failed
                rospy.logerr("TurtleHerder : failed to contact the internal spawn turtle service")
                continue
            except rospy.ROSInterruptException:
                rospy.loginfo("TurtleHerder : shutdown while contacting the internal spawn turtle service")
                continue

    def _launch_turtle_clients(self, turtles):
        # spawn the turtle concert clients
        launch_configurations = prepare_launch_configurations(turtles)
        for launch_configuration in launch_configurations:
            rospy.loginfo("Turtle Herder : launching turtle concert client %s on port %s" %
                      (launch_configuration.name, launch_configuration.port))
            print("%s" % launch_configuration)
            process, meta_roslauncher = self._terminal.spawn_roslaunch_window(launch_configuration)
            print("DJS : meta roslauncher [%s]" % meta_roslauncher.name)
            self._processes.append(process)
            self._temporary_files.append(meta_roslauncher)

    def spawn_turtles(self, turtles):
        unique_turtle_names = self._establish_unique_names(turtles)
        self.turtles.extend(unique_turtle_names)

        self._spawn_simulated_turtles(turtles)
        self._launch_turtle_clients(unique_turtle_names)
        self._send_flip_rules(unique_turtle_names, cancel=False)

    def _establish_unique_names(self, turtles):
        """
        Make sure the turtle names don't clash with currently spawned turtles.
        If they do, postfix them with an incrementing counter.

        :param turtles str[]: list of new turtle names to uniquify.
        :return str[]: uniquified names for the turtles.
        """
        unique_turtle_names = []
        for turtle_name in turtles:
            name_extension = ''
            count = 0
            while turtle_name + name_extension in self.turtles:
                name_extension = '_' + str(count)
                count = count + 1
            unique_turtle_names.append(turtle_name + name_extension)
        return unique_turtle_names

    def _send_flip_rules(self, turtles, cancel):
        for turtle in turtles:
            rules = []
            rule = gateway_msgs.Rule()
            rule.node = ''
            rule.type = gateway_msgs.ConnectionType.SUBSCRIBER
            # could resolve this better by looking up the service info
            rule.name = "/services/turtlesim/%s/cmd_vel" % turtle
            rules.append(copy.deepcopy(rule))
            rule.type = gateway_msgs.ConnectionType.PUBLISHER
            rule.name = "/services/turtlesim/%s/pose" % turtle
            rules.append(copy.deepcopy(rule))
            # send the request
            request = gateway_srvs.RemoteRequest()
            request.cancel = cancel
            remote_rule = gateway_msgs.RemoteRule()
            remote_rule.gateway = turtle
            for rule in rules:
                remote_rule.rule = rule
                request.remotes.append(copy.deepcopy(remote_rule))
            try:
                self._gateway_flip_service(request)
            except rospy.ServiceException:  # communication failed
                rospy.logerr("TurtleHerder : failed to send flip rules")
                return
            except rospy.ROSInterruptException:
                rospy.loginfo("TurtleHerder : shutdown while contacting the gateway flip service")
                return

    def _ros_service_manager_disable_callback(self, msg):
        self.is_disabled = True

    def shutdown(self):
        """
          - Send unflip requests
          - Cleanup turtles on the turtlesim canvas.
          - Shutdown spawned terminals

        :todo: this should go in a service manager callable ros callback where we can
        call disable on this service and bring it down without having to SIGINT it.
        """
        # cleaning turtles is probably not really important since
        # we always shutdown turtlesim and turtle_herder together.
        # for name in self.turtles:
        #     try:
        #         unused_internal_service_response = self._kill_turtle_service_client(name)
        #     except rospy.ServiceException:  # communication failed
        #         break  # quietly fail
        #     except rospy.ROSInterruptException:
        #         break  # quietly fail

        self._terminal.shutdown_roslaunch_windows(processes=self._processes,
                                                  hold=False)
        for temporary_file in self._temporary_files:
            #print("Unlinking %s" % temporary_file.name)
            try:
                os.unlink(temporary_file.name)
            except OSError as e:
                rospy.logerr("Turtle Herder : failed to unlink temporary file [%s]" % str(e))

##############################################################################
# Launch point
##############################################################################

if __name__ == '__main__':

    rospy.init_node('turtle_herder')
    (service_name, unused_service_description, service_priority, unused_service_id) = concert_service_utilities.get_service_info()
    turtles = rospy.get_param('/services/' + service_name + '/turtles', [])
    rospy.loginfo("TurtleHerder: spawning turtles: %s" % turtles)

    turtle_herder = TurtleHerder()
    turtle_herder.spawn_turtles(turtles)
    while not rospy.is_shutdown() and not turtle_herder.is_disabled:
        rospy.sleep(0.3)
    turtle_herder.shutdown()
