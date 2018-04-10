import socket
import threading
import sys
import time
import os
from random import randint

## Zach Bloomquist <bloomquist@gatech.edu>
## Ringo Project Milestone-3
## 12 April 2018
## Networking 1 @ Georgia Tech

PING_TIMEOUT = 15000  # ms

ring_lock = threading.Lock()
rtt_lock = threading.Lock()
rtt_q = []  # queue of pending rtt updates, as (from, raw RTT vector string) tuples
rtt_q_lock = threading.Lock()
rtt_q_sema = threading.Semaphore(0)  # set if an update is pending
last_ping = {}
pinged_at = {}
mt = None

MAX_ACK = 2048
ACK_TIMEOUT = 2000 # ms
ack_counter = 0
acked_nos_lock = threading.Lock()
acked_nos = {}
ack_counter_lock = threading.Lock()

# threading helper class from https://stackoverflow.com/questions/323972/is-there-any-way-to-kill-a-thread-in-python
class StoppableThread(threading.Thread):
    """Thread class with a stop() method. The thread itself has to check
    regularly for the stopped() condition."""

    def __init__(self):
        super(StoppableThread, self).__init__()
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def stopped(self):
        return self._stop_event.is_set()


class KeepAliveThread(StoppableThread):
    name = "KeepAlive"
    def run(self):
        pass  # TODO: implement keepalive once there is network churn


class RttThread(StoppableThread):
    name = "Rtt"
    def run(self):
        global mt, rtt_q, rtt_q_avail, rtt_q_lock, rtt_lock, ring_lock
        while rtt_q_sema.acquire():
            if self.stopped():
                return
            rtt_q_lock.acquire()
            addr, rtt_new = rtt_q.pop()
            rtt_q_lock.release()
            print("Updating RTT matrix") 
            # check to see if we know about all these peers, if not get our RTT to them via ping
            if addr not in mt.rtt[mt.addr]: 
                mt.ping(addr)
            if rtt_new != '':  # this peer knows about other peers
                rtt_new = rtt_new.split(';')
                rtt_new = map(lambda x: x.split(':'), rtt_new)
                row = {}
                for rtt in rtt_new:
                    raddr = (rtt[0], int(rtt[1]))
                    row[raddr] = int(rtt[2])  # generate update for RTT matrix
                    if raddr != mt.addr and raddr not in mt.rtt[mt.addr]:  # new peer
                        mt.ping(raddr)
                rtt_lock.acquire()
                mt.rtt[addr] = row
                rtt_lock.release()
            ring_lock.acquire()  # non-optimal ring
            mt.ring = mt.rtt[mt.addr].keys()
            mt.ring.insert(0, mt.addr)
            ring_lock.release()


class CliThread(StoppableThread):
    name = 'Cli'
    def run(self):
        global mt
        while not self.stopped():
            command = raw_input('').split(' ', 2)
            if command[0] == 'offline':
                if len(command) != 2:
                    print('Usage: offline <T>')
                else:
                    pass  # TODO
            elif command[0] == 'send':
                if len(command) != 2:
                    print('Usage: send <filename>')
                else:
                    pass  # TODO
            elif command[0] == 'show-matrix':
                mt.print_matrix()
            elif command[0] == 'show-ring':
                mt.print_ring()
            elif command[0] == 'disconnect':
                mt.disconnect()
            else:
                print('Supported commands: offline <T>, send <filename>, show-matrix, show-ring, disconnect')


class MainThread(StoppableThread):
    def __init__(self, flag, local_port, poc, n):
        StoppableThread.__init__(self)
        self.name = "Main"
        self.daemon = True
        self.flag = flag
        self.addr = (socket.gethostbyname(socket.gethostname()), int(local_port))
        self.poc = poc
        self.n = n
        self.ringos = []
        self.ring = []
        self.rtt = {
            self.addr: {}
        }  # rtt[from][to]

    def run(self):
        global rtt_q, rtt_q_avail
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(self.addr)
        except:
            print("Could not bind to %s:%d\n" % self.addr)
            return
        print("Ringo running on %s:%d\n" % self.addr)

        self.keepalive_thread = KeepAliveThread()
        self.keepalive_thread.start()
        self.rtt_thread = RttThread()
        self.rtt_thread.start()
        self.cli_thread = CliThread()
        self.cli_thread.start()

        if self.poc != ('0', 0):
            self.sendto("HELO", self.poc)

        while True:
            message, addr = self.sock.recvfrom(2048)
            message = message.rstrip("\r\n")
            parts = message.split(' ', 2)
            seq_no, command = parts[0].split('~', 2)
            seq_no = int(seq_no)
            arguments = ''

            if command != 'ACK':
                self.sendto('ACK %d' % seq_no, addr, False)

            if len(parts) > 1:
                arguments = parts[1]

            print("%s:%d\t(%d)\t-->\t%s %s" % (addr[0], addr[1], seq_no, command, arguments))
    
            if command == "HELO":  # send RTT vector and PING to add to RTT vector
                self.ping(addr)
                self.send_rtt(addr)
            elif command == "PING":  # reply with PONG
                self.sendto("PONG %s" % (arguments), addr)
            elif command == "PONG":  # reset this guy's ping timer
                last_ping[(addr[0], int(addr[1]))] = time.time()*1000
                if addr not in self.rtt[self.addr]:
                    self.rtt[self.addr][addr] = last_ping[addr] - pinged_at[addr]
                    self.broadcast_rtt()
            elif command == "RTT":
                rtt_q_lock.acquire()  # queue RTT vector for investigation
                rtt_q.append(tuple([addr, arguments]))
                rtt_q_lock.release()
                rtt_q_sema.release()
            elif command == "FILE":
                pass
            elif command == "ACK":
                acked_nos_lock.acquire()
                acked_nos[int(arguments)] = True
                acked_nos_lock.release()
            elif command == "BYE":
                pass
            else:
                pass

    def sendto(self, message, dest, expect_ack=True):
        global acked_nos, ack_counter

        ack_counter_lock.acquire()
        i = ack_counter
        ack_counter = (ack_counter + 1) % MAX_ACK
        ack_counter_lock.release()

        print("%s:%d\t(%d)\t<--\t%s" % (dest[0], int(dest[1]), i, message))
        self.sock.sendto("%d~%s\r\n" % (i, message), (dest[0], int(dest[1])))

        if expect_ack:
            acked_nos[i] = False
            t = threading.Timer(ACK_TIMEOUT / 1000, self.check_ack, [message, dest, i])
            t.start()
        
    def check_ack(self, message, dest, i):
        global ack_counter, acked_nos, MAX_ACK, ACK_TIMEOUT
        if not acked_nos[i] or not acked_nos[i]:
            self.sendto(message, dest)

    def ping(self, addr):
        global PING_TIMEOUT
        addr = (addr[0], int(addr[1]))
        if addr not in pinged_at or time.time() * 1000 - pinged_at[addr] > PING_TIMEOUT:
            pinged_at[addr] = time.time() * 1000
            self.sendto("PING", addr)

    def recalculate_ring(self):
        global ring_lock
        minrtt = -1
        for rtt_veci in self.rtt.keys():
            rtt_vec = self.rtt[rtt_veci]
            for rtti in rtt_vec.keys():
                rtt = rtt_vec[rtti]
                if minrtt == -1 or rtt < minrtt:
                    minrtt = rtt
                    route = [rtt_veci]
        def find_min_rtt(starting_at):
            minrtt = -1
            for rtti in self.rtt[starting_at]:
                if rtti in route:
                    continue
                if minrtt == -1 or self.rtt[starting_at][rtti] < minrtt:
                    minrtt = self.rtt[starting_at][rtti]
                    val = rtti
            return (rtti, minrtt)
        while (len(route) < len(self.rtt[self.addr])):
            new_front = find_min_rtt(route[0])
            new_back = find_min_rtt(route[-1])
            if (new_front[1] < new_back[1]):
                route.insert(0, new_front[1])
            else:
                route.append(new_back[1])

        self.ring = route

    def broadcast_rtt(self):
        for peer in self.rtt[self.addr].keys():
                self.send_rtt(peer)

    def send_rtt(self, dest):
        self.sendto("RTT %s" %
            ";".join(
                map(
                    lambda addr: "%s:%d:%d" % 
                    (addr[0], addr[1], self.rtt[self.addr][addr]),
                    self.rtt[self.addr].keys()
                    )
                ),
            dest
        )

    def print_matrix(self):
        print("Current RTT matrix:")
        for peer in self.rtt.keys():
            print("\tRTT vector of %s:%d:" % peer)
            if len(self.rtt[peer]) == 0:
                print("\t\t(empty)")
            for addr in self.rtt[peer].keys():
                rtt = self.rtt[peer][addr]
                print("\t\t%s:%d\t%dms RTT" % (addr[0], addr[1], rtt))
                
    def print_ring(self):
        print("Current ring:")
        if len(self.ring) == 0:
            print("\t(unknown)")
        for addr in self.ring:
            print("\t%s:%d" % addr)

    def disconnect(self):
        self.keepalive_thread.stop()
        self.rtt_thread.stop()
        self.cli_thread.stop()
        self.stop()
        os.kill(os.getpid(), 9)

if __name__ == "__main__":
    if (len(sys.argv) != 6):
        print("Usage: ./ringo <flag> <local-port> <PoC-name> <PoC-port> <N>\n")
        sys.exit(1)
    flag, local_port, poc_name, poc_port, n = sys.argv[1:]
    mt = MainThread(flag, local_port, (poc_name, int(poc_port)), n)
    mt.run()