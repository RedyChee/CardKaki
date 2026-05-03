"""Strip ROS Jazzy from sys.path before pytest plugin discovery.

The host machine has /opt/ros/jazzy/lib/python3.12/site-packages exported
via PYTHONPATH; pytest's entry-point scanner imports launch.frontend which
needs lark, which isn't installed in our venv. Wipe the ROS path so test
collection isn't poisoned.
"""
import sys

sys.path[:] = [p for p in sys.path if "ros/jazzy" not in p]
