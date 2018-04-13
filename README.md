Ringo Milestone 3
========

* Author: Zachary Bloomquist -- bloomquist@gatech.edu
* Date: 12 April 2018

### Files

* README.md - this file
* DESIGN.md - the Design Document
* ringo.py - the Ringo client Python implementation
* sample.txt - Sample output from a 3-member ring
* images/ - timing diagram images for DESIGN.md

### Running the Program

To begin running a Ringo daemon, run the following:

```
python ringo.py <flag> <local-port> <PoC-name> <PoC-port> <N>
```

* flag is one of S for Sender, R for Receiver, or F for forwarder
* local-port is the UDP port we should run on
* PoC-name and PoC-port are used to determine the PoC Ringo to connect to initially - use 0 and 0 to start as a PoC
* N is the maximum number of Ringos when all Ringos are active

### Design Documentation

See DESIGN.md for detailed designs of this project.

### Bugs & Limitations

* "disconnect" command is rough around the edges, just kills process
