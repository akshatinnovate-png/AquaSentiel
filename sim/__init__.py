"""Virtual hardware for AquaSentinel.

This package stands in for the physical rig: a virtual ESP32 running the same
firmware logic we would flash, reading simulated sensors whose values come from
simple physical models rather than from a random number generator.

Why models and not noise: a detector tuned against white noise learns nothing.
A tank that drains through an orifice, a channel that fills from runoff and a
meter that quantises to pulse edges all produce the *shapes* real detectors
have to cope with, which is what makes a measured detection delay meaningful.
"""
