import socket
import threading
import sys
import time
import os
import math
import struct
from random import randint

## Zach Bloomquist <bloomquist@gatech.edu>
## Ringo Project Milestone-3
## 12 April 2018
## Networking 1 @ Georgia Tech

ACK_TIMEOUT = 1500 # ms
PING_TIMEOUT = 10000  # ms
CHUNK_SIZE = 2048

ring_lock = threading.Lock()
rtt_lock = threading.Lock()
rtt_q = []  # queue of pending rtt updates, as (from, raw RTT vector string) tuples
rtt_q_lock = threading.Lock()
rtt_q_sema = threading.Semaphore(0)  # set if an update is pending
ping_timers = {}
dack_timers = []
last_ping_lock = threading.Lock()
last_ping = {}
pinged_at = {}
rtt_knowledge = {} # map of the addrs who have my RTT to the length of the RTT they have
last_msg = {}  # timestamp of last incoming message from a node
mt = None
dack_received = threading.Event()
last_dack = (None, -1)
ack_counter = 0
acked_nos_lock = threading.Lock()
acked_nos = {}
ack_counter_lock = threading.Lock()
last_rtt = {}  # map of host -> sequence number of last RTT received

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

# one of these threads runs for each 
class SendingThread(StoppableThread):
    def run(self):
        global last_dack
        self.unackedringflip = True
        while not self.stopped():
            dack_received.wait()
            if self.stopped():
                return
            if len(mt.chunks) == last_dack[1]:
                mt.stop_sending()
                return
            # check if we need to switch our directions here
            if mt.ringflip and self.unackedringflip:
                self.unackedringflip = False
                print('Ring is flipping')
            # send the chunk with next seqno
            mt.sendraw(mt.chunks[last_dack[1]], mt.ring_next(), last_dack[1] + 1)
            t = threading.Timer(1.5*ACK_TIMEOUT/1000, self.check_dack, [last_dack[1] + 1])
            t.start()
            dack_timers.append(t)
            dack_received.clear()
    def check_dack(self, i):
        if last_dack[1] <= i:  # last sent packet hasn't been acked, resend it
            dack_received.set()


class KeepAliveThread(StoppableThread):
    name = "KeepAlive"
    def __init__(self, addr):
        StoppableThread.__init__(self)
        self.addr = addr
    def run(self):
        global PING_TIMEOUT, ACK_TIMEOUT, last_ping, pinged_at, mt, last_msg
        do = True
        while not self.stopped() and (do or self.addr in mt.rtt[mt.addr]):
            do = False
            mt.ping(self.addr)
            time.sleep((PING_TIMEOUT/2)/1000)
            if self.stopped():
                return
            if self.addr in last_msg and (time.time()*1000) - last_msg[self.addr] > PING_TIMEOUT:
                print('%s:%d has gone offline' % self.addr)
                mt.take_offline(self.addr)


class RttThread(StoppableThread):
    name = "Rtt"
    def run(self):
        global mt, rtt_q, rtt_q_lock, rtt_lock, ring_lock
        while rtt_q_sema.acquire():
            if self.stopped():
                return
            rtt_q_lock.acquire()
            addr, rtt_new = rtt_q.pop()
            rtt_q_lock.release()
            # check to see if we know about all these peers, if not get our RTT to them via ping
            if addr not in mt.rtt[mt.addr]: 
                mt.begin_pinging(addr)
            if rtt_new != '':  # this peer knows about other peers
                rtt_new = rtt_new.split(';')
                rtt_new = map(lambda x: x.split(':'), rtt_new)
                row = {}
                for rtt in rtt_new:
                    raddr = (rtt[0], int(rtt[1]))
                    row[raddr] = int(rtt[2])  # generate update for RTT matrix
                    if raddr != mt.addr and raddr not in mt.rtt[mt.addr]:  # new peer
                        mt.begin_pinging(raddr)
                rtt_lock.acquire()
                mt.rtt[addr] = row
                rtt_lock.release()         
            mt.recalculate_ring()


class CliThread(StoppableThread):
    name = 'Cli'
    def run(self):
        global mt
        while not self.stopped():
            command = raw_input('').split(' ', 2)
            if not mt.online.is_set() and command[0] != 'disconnect':
                print('Ringo is offline, wait before issuing a command')
                continue
            if command[0] == 'offline':
                if len(command) != 2:
                    print('Usage: offline <T>')
                else:
                    mt.go_offline(int(command[1]))
            elif command[0] == 'send':
                if mt.flag != 'S':
                    print('Only the sender Ringo can send a file')
                elif len(command) != 2:
                    print('Usage: send <filename>')
                else:
                    mt.send_file(command[1])
            elif command[0] == 'show-matrix' or command[0] == 'm':
                mt.print_matrix()
            elif command[0] == 'show-ring' or command[0] == 'r':
                mt.print_ring()
            elif command[0] == 'disconnect' or command[0] == 'd':
                mt.disconnect()
            else:
                print('Supported commands: offline <T>, send <filename>, show-matrix, show-ring, disconnect')


class MainThread(StoppableThread):
    def __init__(self, flag, local_port, poc, n):
        StoppableThread.__init__(self)
        self.name = "Main"
        self.flag = flag
        self.addr = (socket.gethostbyname(socket.gethostname()), int(local_port))
        self.poc = poc
        self.n = int(n)
        self.online = threading.Event()
        self.online.set()
        self.reset()

    def reset(self):
        global acked_nos, last_ping, pinged_at, rtt_q, rtt_knowledge, last_rtt
        acked_nos = {}
        last_ping = {}
        pinged_at = {}
        rtt_q = []
        rtt_knowledge = {}
        last_rtt = {}
        self.offline_node = None
        self.sending = False
        self.receiving = False
        self.forwarding = False
        self.ringflip = False
        self.ringos = []
        self.ring = []
        self.rtt = {
            self.addr: {}
        }  # rtt[from][to]

    def run(self):
        global rtt_q, last_rtt, last_dack

        self.rtt_thread = RttThread()
        self.rtt_thread.start()
        self.cli_thread = CliThread()
        self.cli_thread.start()

        while True:
            self.online.wait()

            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.sock.bind(self.addr)
            except:
                print("Could not bind to %s:%d\n" % self.addr)
                return
            print("Ringo running on %s:%d\n" % self.addr)

            if self.poc != ('0', 0):
                self.poc = (socket.gethostbyname(self.poc[0]), self.poc[1])
                self.sendto("HELO", self.poc)


            while self.online.is_set():
                try:
                    try:
                        message, addr = self.sock.recvfrom(65507)
                    except:
                        pass
                    if not self.online.is_set():
                        break
                    addr = (addr[0], int(addr[1]))
                    raw_message = message
                    message = message.rstrip("\r\n")
                    parts = message.split(' ', 2)
                    try:
                        seq_no, command = parts[0].split('~', 2)
                        seq_no = int(seq_no)
                    except:
                        seq_no, command = (0, '')
                    arguments = ''
                    if len(parts) > 1:
                        arguments = parts[1]

                    if command in ('HELO', 'PING', 'PONG', 'RTT', 'FILE', 'DACK', 'BYE', 'ACK'):
                        if command != 'ACK':
                            self.sendto('ACK %d' % seq_no, addr, False)
                        if command not in ('ACK', 'PING', 'PONG'):
                            print("%s:%d\t(%d)\t-->\t%s %s" % (addr[0], addr[1], seq_no, command, arguments))

                    last_msg[addr] = time.time()*1000
            
                    if command == "HELO":  # send RTT vector and PING to add to RTT vector
                        self.begin_pinging(addr)
                        self.send_rtt(addr)
                    elif command == "PING":  # reply with PONG
                        self.sendto("PONG %s" % (arguments), addr)
                        self.send_rtt(addr)
                    elif command == "PONG":  # reset this guy's ping timer
                        last_ping_lock.acquire()
                        last_ping[addr] = time.time()*1000
                        last_ping_lock.release()
                        
                        if addr not in self.rtt[self.addr]:
                            rtt_lock.acquire()
                            self.rtt[self.addr][addr] = (last_ping[addr] - pinged_at[addr]) % ACK_TIMEOUT
                            rtt_lock.release()
                            self.broadcast_rtt()
                        else:
                            self.send_rtt(addr)
                    elif command == "RTT":
                        if not (addr in last_rtt and last_rtt[addr] > seq_no):  # this is a new RTT
                            last_rtt[addr] = seq_no
                            rtt_q_lock.acquire()  # queue RTT vector for investigation
                            rtt_q.append(tuple([addr, arguments]))
                            rtt_q_lock.release()
                            rtt_q_sema.release()
                    elif command == "FILE":
                        if (self.flag == 'R'):  # we've received a FILE from the sender, begin receiving
                            self.sendto('DACK %s:%d:%d' % (self.addr[0], self.addr[1], 0), addr)
                            args = arguments.split(';', 2)
                            self.receive_filename = args[0]
                            self.receive_bytes = 0
                            self.receive_bytes_total = int(args[1])
                            self.received_chunks = {}
                            self.receiving = True
                        elif (self.flag == 'S'):  # FILE made it back around, there's no receiver
                            print('There is no Receiver node in this ring, the file cannot be sent.')
                            self.stop_sending()
                        else:
                            self.forwarding = True
                            self.sendto('FILE %s' % (arguments), self.ring_cont(addr))
                    elif command == 'DACK':
                        if (self.flag == 'S'):  # we've received an ACK from the receiver, notify the sender to continue
                            if self.sending:
                                args = arguments.split(':', 3)
                                if int(args[2]) > last_dack[1]: 
                                    last_dack = ((args[0], int(args[1])), int(args[2]))
                                    dack_received.set()
                        elif self.flag != 'R':
                            self.forwarding = True
                            self.sendto('DACK %s' % (arguments), self.ring_cont(addr))
                    elif command == "ACK":
                        acked_nos_lock.acquire()
                        acked_nos[int(arguments)] = True
                        acked_nos_lock.release() 
                    elif command == "BYE":
                        if (self.flag == 'R' and self.receiving):
                            self.receive_complete()
                        elif self.forwarding:
                            self.forwarding = False
                            self.sendto('BYE', self.ring_cont(addr))
                    else:
                        # data packet
                        seq_no = struct.unpack('I', raw_message[0:4])[0]
                        data = raw_message[4:]
                        if self.flag == 'R':
                            if self.receiving:
                                self.sendto('DACK %s:%d:%d' % (self.addr[0], self.addr[1], seq_no), addr)
                                if seq_no not in self.received_chunks:
                                    self.receive_bytes += len(data)
                                self.received_chunks[seq_no] = data
                                print("%s:%d\t[%d]\t-->\t[RAW DATA] %d bytes (%.2f%% received)" % (addr[0], addr[1], seq_no, len(raw_message), 100*float(self.receive_bytes)/self.receive_bytes_total))
                        else:
                            print("%s:%d\t[%d]\t-->\t[RAW DATA] %d bytes" % (addr[0], addr[1], seq_no, len(raw_message)))
                            self.forwarding = True
                            self.sendraw(data, self.ring_cont(addr), seq_no)
                except:
                    print('Error coming back online, continuing')

    def sendto(self, message, dest, expect_ack=True, ackno=0):
        global acked_nos, ack_counter
        if ackno == 0:
            ack_counter_lock.acquire()
            i = ack_counter
            ack_counter = (ack_counter + 1)
            ack_counter_lock.release()
        else:
            i = ackno
        if message[0:4] not in ('PING', 'PONG') and message[0:3] not in ('ACK'):
            print("%s:%d\t(%d)\t<--\t%s" % (dest[0], int(dest[1]), i, message))
        self.sock.sendto("%d~%s\r\n" % (i, message), (dest[0], int(dest[1])))
        if expect_ack and i not in acked_nos:
            acked_nos[i] = False
            t = threading.Timer(ACK_TIMEOUT / 1000, self.check_ack, [message, dest, i])
            t.start()

    def sendraw(self, data, dest, seqno):
        print("%s:%d\t[%d]\t<--\t[RAW DATA] %d bytes" % (dest[0], int(dest[1]), seqno, len(data) + 4))
        self.sock.sendto(struct.pack('I', seqno) + data, dest)

    def send_file(self, filename):
        global CHUNK_SIZE
        try:
            f = open(filename, 'rb')
            whole_file = f.read()
        except:
            print("Could not read file %s" % (filename))
            return
        self.total_size = len(whole_file)
        self.chunks = []
        for i in range(0, self.total_size, CHUNK_SIZE):
            self.chunks.append(whole_file[i:i+CHUNK_SIZE])
        self.sending = True
        self.sendto('FILE %s;%d' % (filename, self.total_size), self.ring_next())
        self.sending_thread = SendingThread()
        self.sending_thread.start()

    def stop_sending(self):
        global last_dack, dack_received, dack_timers
        del self.total_size
        del self.chunks
        self.sending = False
        self.sending_thread.stop()
        self.sending_thread = None
        last_dack = ('', -1)
        self.sendto('BYE', self.ring_next())
        for dack_timer in dack_timers:
            try:
                dack_timer.cancel()
            except:
                pass
        dack_timers = []
        dack_received.clear()
        self.ringflip = False

    def receive_complete(self):
        data = ''
        for i in sorted(self.received_chunks.keys()):
            data += self.received_chunks[i]
        filename = self.receive_filename
        i = 1
        while os.path.isfile(filename):  # do not override existing file
            filename = self.receive_filename + ('.%d' % (i))
            i += 1
        try:
            f = open(filename, 'wb+')
            f.write(data)
            f.close()
            print('%s successfully saved.' % (filename))
        except:
            pass
        self.receive_filename = ''
        self.receive_bytes = 0
        self.receive_bytes_total = 0
        self.received_chunks = {}
    
    def go_offline(self, seconds):
        global ping_timers
        self.online.clear()
        self.sock.setblocking(0)
        for ping_timer in ping_timers:
            ping_timers[ping_timer].stop()
        ping_timers = {}
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except:
            pass
        self.sock.close()
        self.reset()
        print("Going offline for %d seconds" % seconds)
        t = threading.Timer(seconds, self.go_online)
        t.start()

    def go_online(self):
        print("Back online")
        self.online.set()

    def check_ack(self, message, dest, i):
        global ack_counter, acked_nos, ACK_TIMEOUT
        if i not in acked_nos or (dest in last_msg and (time.time() * 1000) - last_msg[dest] > PING_TIMEOUT):
            pass
        elif not acked_nos[i]:
            if message[0:4] == 'PING':
                self.ping(dest, i)
            else:
                self.sendto(message, dest, True, i)
        else:
            if message[0:3] == 'RTT':
                rtt_knowledge[dest] = message.count(';') + 1
            del acked_nos[i]

    def begin_pinging(self, addr):
        if self.poc == ('0', 0):
            self.poc = addr
        if addr in ping_timers:
            return
        if self.offline_node == addr:
            self.offline_node = None
        ping_timers[addr] = KeepAliveThread(addr)
        ping_timers[addr].start()

    def take_offline(self, addr):
        global pinged_at, last_ping, ping_timers, rtt_knowledge
        rtt_lock.acquire()
        self.offline_node = addr
        if addr in self.rtt:
            del self.rtt[addr]
        for fromaddr in self.rtt:
            if addr in self.rtt[fromaddr]:
                del self.rtt[fromaddr][addr]
        rtt_lock.release()
        if addr in ping_timers:
            ping_timers[addr].stop()
            del ping_timers[addr]
        if addr in pinged_at:
            del pinged_at[addr]
        if addr in last_ping:
            del last_ping[addr]
        if addr in last_rtt:
            del last_rtt[addr]
        if addr in last_msg:
            del last_msg[addr]
        self.broadcast_rtt()
        self.recalculate_ring()
        if self.sending:
            self.ringflip = self.ring_needs_switching(addr)

    def ping(self, addr, seqno=0):
        global PING_TIMEOUT
        addr = (addr[0], int(addr[1]))
        #if addr not in pinged_at or time.time() * 1000 - pinged_at[addr] > PING_TIMEOUT or resend:
        pinged_at[addr] = time.time() * 1000
        self.sendto("PING", addr, True, seqno)

    def ring_prev(self):
        i = (self.ring.index(self.addr) - 1) % len(self.ring)
        if self.ring[i] == self.offline_node:
            return self.ring[i - 1]
        return self.ring[i]

    def ring_next(self):
        if self.ringflip:
            return self.ring_prev()
        i = (self.ring.index(self.addr) + 1) % len(self.ring)
        if self.ring[i] == self.offline_node:
            return self.ring[i + 1]
        return self.ring[i]

    def ring_needs_switching(self, offline_node):
        global last_dack
        end_node = last_dack[0]
        if not end_node or not self.is_transmitting():
            return False
        i = self.ring.index(self.addr)
        while self.ring[i] != end_node:
            i = (i + 1) % len(self.ring)
            if self.ring[i] == offline_node:
                return True
        return False

    def ring_cont(self, addr):
        if addr != self.ring_prev():
            return self.ring_prev()
        else:
            return self.ring_next()

    def is_transmitting(self):
        return self.forwarding or self.sending or self.receiving

    def recalculate_ring(self):
        global ring_lock
        #if self.n == len(self.ring) and self.is_transmitting():
        #    return
        if len(self.rtt) < 2:
            return
        # seed the route with the lowest possible from->to
        minrtt = float('Inf')
        route = []
        for fromaddr in self.rtt:
            rtts = self.rtt[fromaddr]
            for toaddr in rtts:
                rtt = rtts[toaddr]
                if rtt < minrtt or (rtt == minrtt and toaddr < route[1]):
                    route = [fromaddr, toaddr]
                    minrtt = rtt
        # add the lowest rtts not in list
        def find_min_rtt(starting_at):
            minrtt = float('Inf')
            node = starting_at
            if starting_at not in self.rtt:
                return (minrtt, starting_at)
            for rtti in self.rtt[starting_at]:
                if rtti in route:
                    continue
                if self.rtt[starting_at][rtti] < minrtt or (self.rtt[starting_at][rtti] == minrtt and rtti < node):
                    minrtt = self.rtt[starting_at][rtti]
                    node = rtti
            return (node, minrtt)
        while (len(route) < len(self.rtt)):
            new_front = find_min_rtt(route[0])
            new_back = find_min_rtt(route[-1])
            if (new_front[1] < new_back[1]):
                route.insert(0, new_front[0])
            else:
                route.append(new_back[0])

        ring_lock.acquire()
        self.ring = route
        ring_lock.release()

    def broadcast_rtt(self):
        rtt_lock.acquire()
        for peer in self.rtt[self.addr].keys():
                self.send_rtt(peer)
        rtt_lock.release()

    def send_rtt(self, dest):
        global rtt_knowledge
        if len(self.rtt[self.addr]) == 0:
            return
        if dest in rtt_knowledge and len(self.rtt[self.addr]) == rtt_knowledge[dest]:
            return
        #rtt_knowledge[dest] = len(self.rtt[self.addr])
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
        # TODO stop all ping threads
        for ping_timer in ping_timers:
            ping_timers[ping_timer].stop()
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