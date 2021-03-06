#!/usr/bin/env python
from os.path import join
from rospkg import RosPack
from threading import Thread, RLock
from sys import exit
import json
import rospy

from pypot.creatures import PoppyErgoJr

from poppy_msgs.srv import ExecuteTrajectory, SetCompliant, ExecuteTrajectoryResponse, SetCompliantResponse, ReachTarget, ReachTargetResponse
from geometry_msgs.msg import PoseStamped
from sensor_msgs.msg import JointState


class ErgoJrControllers(object):
    def __init__(self, robot_name):
        """
        :param robot_name: Robot name and ROS topics/services namespace
        """
        self.rospack = RosPack()
        with open(join(self.rospack.get_path('poppy_ergo_jr_controllers'), 'config', 'ergo_jr.json')) as f:
            self.params = json.load(f)

        self.publish_rate = rospy.Rate(self.params['publish_rate'])
        self.robot_name = robot_name

        self.eef_pub = rospy.Publisher('end_effector_pose', PoseStamped, queue_size=1)
        self.js_pub = rospy.Publisher('joint_state', JointState, queue_size=1)
        self.goal_pub = rospy.Publisher('joint_goals', JointState, queue_size=1)

        # Services
        self.srv_robot_target = None
        self.srv_robot_execute = None
        self.srv_robot_set_compliant = None

        # Protected resources
        self.ergo = None
        self.robot_lock = RLock()
    
    def reset_max_speed(self):
        # Reseting max speed to 100%
        for m in self.ergo.motors:
            m.moving_speed = 100
    
    def run(self, simulator=None):
        rospy.loginfo("Controller is connecting to {}...".format(self.robot_name))
        port = rospy.get_param('vrep/port', 19997)
        try:
            config = rospy.get_param('description')
            config_file = rospy.get_param('description_file') 
        except KeyError:
            rospy.logerr("Expected robot description in parameter 'description' and file path in 'description_file', please use the launch file to load them")
            exit(-1)
        try:
            self.ergo = PoppyErgoJr(config=config_file, use_http=True, simulator=simulator, scene="keep-existing", port=port)
        except IOError as e:
            rospy.logerr("{} failed to init: {}".format(self.robot_name, e))
            exit(-1)
        else:
            self.ergo.power_up()
            self.ergo.compliant = False
            self.reset_max_speed()
            ########################## Setting up services
            self.srv_robot_execute = rospy.Service('execute', ExecuteTrajectory, self._cb_execute)
            self.srv_robot_target = rospy.Service('reach', ReachTarget, self._cb_reach)
            self.srv_robot_set_compliant = rospy.Service('set_compliant', SetCompliant, self._cb_set_compliant)

            rospy.loginfo("{} controllers are up!".format(self.robot_name))

            while not rospy.is_shutdown():
                self.publish_goals()
                self.publish_eef(self.ergo.chain.end_effector)
                self.publish_js()
                self.publish_rate.sleep()
        finally:
            if self.ergo is not None:
                self.ergo.close()

    def publish_eef(self, eef_pose):
        pose = PoseStamped()
        pose.header.stamp = rospy.Time.now()
        pose.header.frame_id = 'ergo_jr_base'
        pose.pose.position.x = eef_pose[0]
        pose.pose.position.y = eef_pose[1]
        pose.pose.position.z = eef_pose[2]
        self.eef_pub.publish(pose)

    def publish_js(self):
        js = JointState()
        js.header.stamp = rospy.Time.now()
        js.name = [m.name for m in self.ergo.motors]
        js.position = [m.present_position for m in self.ergo.motors]
        js.velocity = [m.present_speed for m in self.ergo.motors]
        js.effort = [m.present_load for m in self.ergo.motors]
        self.js_pub.publish(js)

    def publish_goals(self):
        js = JointState()
        js.header.stamp = rospy.Time.now()
        js.name = [m.name for m in self.ergo.motors]
        js.position = [m.goal_position for m in self.ergo.motors]
        js.velocity = [m.goal_speed for m in self.ergo.motors]
        self.goal_pub.publish(js)

    def _cb_execute(self, request):
        # TODO Action server
        thread = Thread(target=self.execute, args=[request.trajectory])
        thread.daemon = True
        thread.start()
        return ExecuteTrajectoryResponse()

    def _cb_reach(self, request):
        target = dict(zip(request.target.name, request.target.position))
        duration = request.duration.to_sec()
        if duration <= 0.:
            rospy.loginfo("Teleporting {}...".format(str(target)))
        else:
            rospy.loginfo("Reaching {} in {} sec...".format(str(target), duration))

        with self.robot_lock:
            if duration <= 0.:
                # Duration = 0 teleports the joint or goes at full speed
                for motor in request.target.name:
                    try:
                        index = ['m1', 'm2', 'm3', 'm4', 'm5', 'm6'].index(motor)
                    except ValueError:
                        pass
                    else:
                        self.ergo.motors[index].goal_position = target[motor]
            else:
                # Other durations will trigger a threaded control
                self.ergo.goto_position(target, request.duration.to_sec())
                self.reset_max_speed()

        return ReachTargetResponse()

    def execute(self, trajectory):
        with self.robot_lock:
            rospy.loginfo("Executing Ergo Jr trajectory with {} points...".format(len(trajectory.points)))
            time = 0.
            try:
                for point_id, point in enumerate(trajectory.points):
                    if rospy.is_shutdown():
                        break

                    time_from_start = point.time_from_start.to_sec()
                    duration = time_from_start - time

                    if duration < 0.:
                        rospy.logwarn("Skipping invalid point {}/{} with incoherent time_from_start", point_id + 1, len(trajectory.points))
                        continue

                    self.ergo.goto_position(dict(zip(trajectory.joint_names, point.positions)),
                                            self.params['time_margin'] + duration)  # Time margin trick to smooth trajectory
                    rospy.sleep(duration - 0.001)
                    time = time_from_start
            except rospy.exceptions.ROSInterruptException:
                rospy.logwarn("Trajectory aborted!")
            else:
                rospy.loginfo("Trajectory ended!")

    def _cb_set_compliant(self, request):
        rospy.loginfo("{} now {}".format(self.robot_name, 'compliant' if request.compliant else 'rigid'))
        with self.robot_lock:
            for m in self.ergo.motors:
                m.compliant = request.compliant
        return SetCompliantResponse()


if __name__ == '__main__':
    rospy.init_node("poppy_ergo_jr_controllers")
    simulator = rospy.get_param("simulator", None)
    if simulator == "none":
        simulator = None
    ErgoJrControllers(rospy.get_namespace().strip('/')).run(simulator=simulator)
