#!/usr/bin/python
# -*- coding: latin-1 -*-
# -----------------------------------------------------------------------------
# Copyright 2010-2013 Stephen Tiedemann <stephen.tiedemann@gmail.com>
#
# Licensed under the EUPL, Version 1.1 or - as soon they 
# will be approved by the European Commission - subsequent
# versions of the EUPL (the "Licence");
# You may not use this work except in compliance with the
# Licence.
# You may obtain a copy of the Licence at:
#
# http://www.osor.eu/eupl
#
# Unless required by applicable law or agreed to in
# writing, software distributed under the Licence is
# distributed on an "AS IS" basis,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied.
# See the Licence for the specific language governing
# permissions and limitations under the Licence.
# -----------------------------------------------------------------------------

import logging
log = logging.getLogger()

import os
import sys
import time
import string
import struct
import os.path
import inspect
import argparse
import Queue as queue
from threading import Thread, Lock

sys.path.insert(1, os.path.split(sys.path[0])[0])
from cli import CommandLineInterface, TestError

import nfc
import nfc.ndef
import nfc.llcp

def info(message, prefix="  "):
    log.info(prefix + message)

def trace(func):
    def traced_func(*args, **kwargs):
        _args = "{0}".format(args[1:]).strip("(),")
        if kwargs:
            _args = ', '.join([_args, "{0}".format(kwargs).strip("{}")])
        log.info("{func}({args})".format(func=func.__name__, args=_args))
        return func(*args, **kwargs)
    return traced_func

def printable(data):
    printable = string.digits + string.letters + string.punctuation + ' '
    return ''.join([c if c in printable else '.' for c in data])

def format_data(data):
    s = []
    for i in range(0, len(data), 16):
        s.append("  %04x: " % i)
        s[-1] += ' '.join(["%02x" % ord(c) for c in data[i:i+16]]) + ' '
        s[-1] += (8 + 16*3 - len(s[-1])) * ' '
        s[-1] += printable(data[i:i+16])
    return '\n'.join(s)

class PhdcAgent(Thread):
    def __init__(self):
        super(PhdcAgent, self).__init__()
        self.oqueue = queue.Queue()
        self.iqueue = queue.Queue()

    def enqueue(self, apdu):
        if apdu is None or len(apdu) > 0:
            self.iqueue.put(apdu)

    def dequeue(self, timeout):
        try:
            apdu = self.oqueue.get(block=True, timeout=timeout)
        except queue.Empty:
            apdu = ""
        return apdu
                
    def send(self, apdu):
        log.info("[ieee] >>> {0}".format(str(apdu).encode("hex")))
        self.oqueue.put(apdu)

    def recv(self, timeout):
        try:
            apdu = self.iqueue.get(block=True, timeout=timeout)
        except queue.Empty:
            pass
        else:
            log.info("[ieee] <<< {0}".format(str(apdu).encode("hex")))
            return apdu

class PhdcTagAgent(PhdcAgent):
    def __init__(self, tag, cmd, apdu=bytearray(), flags='\x00'):
        super(PhdcTagAgent, self).__init__()
        self.terminate = False
        self.mc = 1
        attr = nfc.tt3.NdefAttributeData()
        attr.version = "1.0"
        attr.nbr, attr.nbw = 12, 8
        attr.capacity = 1024
        attr.writeable = True
        attr.length = 7 + len(apdu)
    
        phd_rec = nfc.ndef.Record("urn:nfc:wkt:PHD", data=flags + apdu)
        phd_msg = nfc.ndef.Message(phd_rec)
        
        self.ndef_data_area = str(attr) + bytearray(attr.capacity)
        self.ndef_data_area[16:16+7+len(apdu)] = bytearray(str(phd_msg))

        tag.add_service(0x0009, self.ndef_read, self.ndef_write)
        tag.add_service(0x000B, self.ndef_read, lambda: False)
        self.tag = tag
        self.cmd = cmd
        
        self.ndef_read_lock = Lock()
        self.ndef_write_lock = Lock()

    def ndef_read(self, block, read_begin, read_end):
        if read_begin is True:
            self.ndef_read_lock.acquire()
        try:
            if block < len(self.ndef_data_area) / 16:
                data = self.ndef_data_area[block*16:(block+1)*16]
                log.debug("[tt3] got read block #{0} {1}".format(
                        block, str(data).encode("hex")))
                return data
            else:
                log.debug("[tt3] got read block #{0}".format(block))
        finally:
            if read_end is True:
                self.ndef_read_lock.release()
    
    def ndef_write(self, block, data, write_begin, write_end):
        if write_begin is True:
            self.ndef_write_lock.acquire()
        try:
            log.debug("[tt3] got write block #{0} {1}".format(
                    block, str(data).encode("hex")))
            if block < len(self.ndef_data_area) / 16:
                self.ndef_data_area[block*16:(block+1)*16] = data
                return True
        finally:
            if write_end is True:
                self.ndef_write_lock.release()
                apdu = self.recv_phd_message()
                if apdu is not None:
                    self.enqueue(apdu)
                    Thread(target=self.send_phd_message).start()
            
    def recv_phd_message(self):
        attr = nfc.tt3.NdefAttributeData(self.ndef_data_area[0:16])
        if attr.valid and not attr.writing and attr.length > 0:
            print str(self.ndef_data_area[16:16+attr.length]).encode("hex")
            try:
                message = nfc.ndef.Message(
                    self.ndef_data_area[16:16+attr.length])
            except nfc.ndef.LengthError:
                return None

            if message.type == "urn:nfc:wkt:PHD":
                data = bytearray(message[0].data)
                if data[0] & 0x8F == 0x80 | (self.mc % 16):
                    log.info("[phdc] <<< " + str(data).encode("hex"))
                    self.mc += 1
                    attr.length = 0
                    self.ndef_data_area[0:16] = bytearray(str(attr))
                    return data[1:]
                   
    def send_phd_message(self):
        apdu = self.dequeue(timeout=0.1)
        data = bytearray([0x80 | (self.mc % 16)]) + apdu
        record = nfc.ndef.Record("urn:nfc:wkt:PHD", data=str(data))
        with self.ndef_read_lock:
            log.info("[phdc] >>> " + str(data).encode("hex"))
            data = bytearray(str(nfc.ndef.Message(record)))
            attr = nfc.tt3.NdefAttributeData(self.ndef_data_area[0:16])
            attr.length = len(data)
            self.ndef_data_area[0:16+attr.length] = str(attr) + data
            self.mc += 1
        
    def run(self):
        log.info("entering phdc agent run loop")
        command, self.cmd = self.cmd, None
        while not (command is None or self.terminate is True):
            response = self.tag.process_command(command)
            try:
                command = self.tag.send_response(response, timeout=1)
            except nfc.clf.TimeoutError:
                log.info("no command received within 10 seconds")
                break
            except nfc.clf.TransmissionError:
                break
        log.info("leaving phdc agent run loop")

    def stop(self):
        self.terminate = True
        self.join(timeout=10.0)
        
thermometer_assoc_req = \
    "E200 0032 8000 0000" \
    "0001 002A 5079 0026" \
    "8000 0000 8000 8000" \
    "0000 0000 0000 0080" \
    "0000 0008 3132 3334" \
    "3536 3738 0320 0001" \
    "0100 0000 0000"

thermometer_assoc_res = \
    "E300 002C 0003 5079" \
    "0026 8000 0000 8000" \
    "8000 0000 0000 0000" \
    "8000 0000 0008 3837" \
    "3635 3433 3231 0000" \
    "0000 0000 0000 0000" \

assoc_release_req = "E40000020000"
assoc_release_res = "E50000020000"

def phdc_tag_agent(args):
    log.info("performing as tag agent")
    if args.test == 0:
        phdc_tag_agent_test0(args)
    if args.test == 1:
        phdc_tag_agent_test1(args)
    if args.test == 2:
        phdc_tag_agent_test2(args)
    if args.test == 3:
        phdc_tag_agent_test3(args)
    if args.test == 4:
        phdc_tag_agent_test4(args)

def phdc_tag_agent_test0(args):
    idm = bytearray.fromhex("02FE") + os.urandom(6)
    pmm = bytearray.fromhex("01E0000000FFFF00")
    sys = bytearray.fromhex("12FC")
    target = nfc.clf.TTF(br=None, idm=idm, pmm=pmm, sys=sys)
                       
    log.info("touch a manager")

    while True:
        activated = args.clf.listen([target], timeout=1)
        if activated:
            log.info("tag activated")
            target, command = activated
            tag = nfc.tt3.Type3TagEmulation(args.clf, target)
            agent = PhdcTagAgent(tag, command)
            agent.start(command)
            log.info("entering ieee agent")

            with open("scenario.txt") as f:
                for line in f:
                    if line.startswith('#'):
                        continue
                    apdu = bytearray.fromhex(line.strip())
                    agent.send(apdu)
                    apdu = agent.recv(timeout=5.0)
                    if apdu is None:
                        break
            
            log.info("leaving ieee agent")
            break

    if agent.is_alive():
        agent.stop()
        
def phdc_tag_agent_test1(args):
    idm = bytearray.fromhex("02FE") + os.urandom(6)
    pmm = bytearray.fromhex("01E0000000FFFF00")
    sys = bytearray.fromhex("12FC")
    target = nfc.clf.TTF(br=None, idm=idm, pmm=pmm, sys=sys)
                       
    log.info("touch a manager")

    while True:
        activated = args.clf.listen([target], timeout=1)
        if activated:
            log.info("tag activated")
            target, command = activated
            tag = nfc.tt3.Type3TagEmulation(args.clf, target)
            agent = PhdcTagAgent(tag, command)
            agent.start()
            log.info("entering ieee agent")
            
            apdu = bytearray.fromhex(thermometer_assoc_req)
            log.info("send thermometer association request")
            agent.send(apdu)

            apdu = agent.recv(timeout=5.0)
            if apdu is None:
                break
            
            if apdu.startswith("\xE3\x00"):
                log.info("rcvd association response")
            
            time.sleep(3.0)
            
            apdu = bytearray.fromhex(assoc_release_req)
            log.info("send association release request")
            agent.send(apdu)
                
            apdu = agent.recv(timeout=5.0)
            if apdu is None:
                break
            
            if apdu.startswith("\xE5\x00"):
                log.info("rcvd association release response")
            
            log.info("leaving ieee agent")
            break

    if agent.is_alive():
        agent.stop()
        
def phdc_tag_agent_test2(args):
    idm = bytearray.fromhex("02FE") + os.urandom(6)
    pmm = bytearray.fromhex("01E0000000FFFF00")
    sys = bytearray.fromhex("12FC")
    target = nfc.clf.TTF(br=None, idm=idm, pmm=pmm, sys=sys)
                       
    log.info("touch a manager")

    while True:
        activated = args.clf.listen([target], timeout=1)
        if activated:
            log.info("tag activated")
            target, command = activated
            tag = nfc.tt3.Type3TagEmulation(args.clf, target)
            agent = PhdcTagAgent(tag, command)
            agent.start()
            log.info("entering ieee agent")
            
            apdu = bytearray.fromhex(thermometer_assoc_req)
            log.info("send thermometer association request")
            agent.send(apdu)
            
            apdu = agent.recv(timeout=5.0)
            if apdu is None: break
            if apdu.startswith("\xE3\x00"):
                log.info("rcvd association response")
            
            apdu = bytearray.fromhex(assoc_release_req)
            log.info("send association release request")
            agent.send(apdu)
                
            apdu = agent.recv(timeout=5.0)
            if apdu is None: break
            if apdu.startswith("\xE5\x00"):
                log.info("rcvd association release response")
            
            log.info("leaving ieee agent")

            time.sleep(3.0)

            log.info("entering ieee agent")
            
            apdu = bytearray.fromhex(thermometer_assoc_req)
            log.info("send thermometer association request")
            agent.send(apdu)
            
            apdu = agent.recv(timeout=5.0)
            if apdu is None: break
            if apdu.startswith("\xE3\x00"):
                log.info("rcvd association response")
            
            time.sleep(1.0)
            log.info("now move devices out of communication range")
            
            log.info("leaving ieee agent")
            agent.join(timeout=10.0)
            break
        
def phdc_tag_agent_test3(args):
    idm = bytearray.fromhex("02FE") + os.urandom(6)
    pmm = bytearray.fromhex("01E0000000FFFF00")
    sys = bytearray.fromhex("12FC")
    target = nfc.clf.TTF(br=None, idm=idm, pmm=pmm, sys=sys)
                       
    log.info("touch a manager")

    while True:
        activated = args.clf.listen([target], timeout=1)
        if activated:
            log.info("tag activated")
            target, command = activated
            tag = nfc.tt3.Type3TagEmulation(args.clf, target)
            agent = PhdcTagAgent(tag, command, flags='\x02')
            log.info("sending with non-zero message counter")
            agent.start()
            agent.join(timeout=10.0)
            break
        
def phdc_tag_agent_test4(args):
    idm = bytearray.fromhex("02FE") + os.urandom(6)
    pmm = bytearray.fromhex("01E0000000FFFF00")
    sys = bytearray.fromhex("12FC")
    target = nfc.clf.TTF(br=None, idm=idm, pmm=pmm, sys=sys)
                       
    log.info("touch a manager")

    while True:
        activated = args.clf.listen([target], timeout=1)
        if activated:
            log.info("tag activated")
            target, command = activated
            tag = nfc.tt3.Type3TagEmulation(args.clf, target)
            agent = PhdcTagAgent(tag, command, flags='\x40')
            log.info("sending with non-zero reserved field")
            agent.start()
            
            log.info("entering ieee agent")
            time.sleep(3.0)
            log.info("leaving ieee agent")
            agent.join(timeout=10.0)
            break
        
description = """
Execute some Personal health Device Communication (PHDC) tests. The
peer device must have the PHDC validation test server running.
"""
class TestProgram(CommandLineInterface):
    def __init__(self):
        parser = argparse.ArgumentParser(
            usage='%(prog)s [OPTION]...',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=description)
        super(TestProgram, self).__init__(parser, groups="tst p2p dbg clf")

    def test_00(self, llc):
        """Send data read from scenario file"""

        socket = llc.socket(nfc.llcp.DATA_LINK_CONNECTION)
        llc.setsockopt(socket, nfc.llcp.SO_RCVBUF, 2)
        llc.connect(socket, "urn:nfc:sn:phdc")
        peer_sap = llc.getpeername(socket)
        log.info("connected with phdc manager at sap {0}".format(peer_sap))
        log.info("entering ieee agent")

        try:
            with open("scenario.txt") as f:
                for line in f:
                    if line.startswith('#'):
                        continue

                    apdu = bytearray.fromhex(line)
                    apdu = struct.pack(">H", len(apdu)) + apdu
                    log.info("send {0}".format(str(apdu).encode("hex")))
                    llc.send(socket, str(apdu))

                    apdu = llc.recv(socket)
                    log.info("rcvd {0}".format(str(apdu).encode("hex")))
        except IOError as e:
            log.error(e)

        log.info("leaving ieee agent")
        llc.close(socket)

    def test_01(self, llc):
        """Connect, associate and release"""
        
        socket = llc.socket(nfc.llcp.DATA_LINK_CONNECTION)
        llc.setsockopt(socket, nfc.llcp.SO_RCVBUF, 2)
        service_name = "urn:nfc:sn:phdc"
        try:
            llc.connect(socket, service_name)
        except nfc.llcp.ConnectRefused:
            raise TestError("could not connect to {0!r}".format(service_name))
        
        peer_sap = llc.getpeername(socket)
        info("connected with phdc manager at sap {0}".format(peer_sap))
        info("entering ieee agent")

        apdu = bytearray.fromhex(thermometer_assoc_req)
        apdu = struct.pack(">H", len(apdu)) + apdu
        info("send thermometer association request")
        info("send {0}".format(str(apdu).encode("hex")))
        llc.send(socket, str(apdu))

        apdu = llc.recv(socket)
        info("rcvd {0}".format(str(apdu).encode("hex")))
        if apdu.startswith("\xE3\x00"):
            info("rcvd association response")

        time.sleep(3.0)

        apdu = bytearray.fromhex(assoc_release_req)
        apdu = struct.pack(">H", len(apdu)) + apdu
        info("send association release request")
        info("send {0}".format(str(apdu).encode("hex")))
        llc.send(socket, str(apdu))

        apdu = llc.recv(socket)
        info("rcvd {0}".format(str(apdu).encode("hex")))
        if apdu.startswith("\xE5\x00"):
            info("rcvd association release response")

        info("leaving ieee agent")
        llc.close(socket)

    def test_02(self, llc):
        """Association after release"""

        socket = llc.socket(nfc.llcp.DATA_LINK_CONNECTION)
        llc.setsockopt(socket, nfc.llcp.SO_RCVBUF, 2)
        service_name = "urn:nfc:sn:phdc"
        try:
            llc.connect(socket, service_name)
        except nfc.llcp.ConnectRefused:
            raise TestError("could not connect to {0!r}".format(service_name))
        
        peer_sap = llc.getpeername(socket)
        info("connected with phdc manager at sap {0}".format(peer_sap))
        info("entering ieee agent")

        apdu = bytearray.fromhex(thermometer_assoc_req)
        apdu = struct.pack(">H", len(apdu)) + apdu
        info("send thermometer association request")
        info("send {0}".format(str(apdu).encode("hex")))
        llc.send(socket, str(apdu))

        apdu = llc.recv(socket)
        info("rcvd {0}".format(str(apdu).encode("hex")))
        if apdu.startswith("\xE3\x00"):
            info("rcvd association response")

        llc.close(socket)

        socket = llc.socket(nfc.llcp.DATA_LINK_CONNECTION)
        llc.setsockopt(socket, nfc.llcp.SO_RCVBUF, 2)
        llc.connect(socket, "urn:nfc:sn:phdc")
        peer_sap = llc.getpeername(socket)
        info("connected with phdc manager at sap {0}".format(peer_sap))
        info("entering ieee agent")

        apdu = bytearray.fromhex(thermometer_assoc_req)
        apdu = struct.pack(">H", len(apdu)) + apdu
        info("send thermometer association request")
        info("send {0}".format(str(apdu).encode("hex")))
        llc.send(socket, str(apdu))

        apdu = llc.recv(socket)
        info("rcvd {0}".format(str(apdu).encode("hex")))
        if apdu.startswith("\xE3\x00"):
            info("rcvd association response")

        time.sleep(3.0)

        apdu = bytearray.fromhex(assoc_release_req)
        apdu = struct.pack(">H", len(apdu)) + apdu
        info("send association release request")
        info("send {0}".format(str(apdu).encode("hex")))
        llc.send(socket, str(apdu))

        apdu = llc.recv(socket)
        info("rcvd {0}".format(str(apdu).encode("hex")))
        if apdu.startswith("\xE5\x00"):
            info("rcvd association release response")

        info("leaving ieee agent")

    def test_03(self, llc):
        """Fragmentation and reassembly"""
        
        socket = llc.socket(nfc.llcp.DATA_LINK_CONNECTION)
        llc.setsockopt(socket, nfc.llcp.SO_RCVBUF, 2)
        service_name = "urn:nfc:xsn:nfc-forum.org:phdc-validation"
        try:
            llc.connect(socket, service_name)
        except nfc.llcp.ConnectRefused:
            raise TestError("could not connect to {0!r}".format(service_name))
        
        peer_sap = llc.getpeername(socket)
        info("connected with phdc manager at sap {0}".format(peer_sap))

        miu = llc.getsockopt(socket, nfc.llcp.SO_SNDMIU)
        
        apdu = os.urandom(2176)
        log.info("send ieee apdu of size {0} byte".format(len(apdu)))
        apdu = struct.pack(">H", len(apdu)) + apdu
        for i in range(0, len(apdu), miu):
            llc.send(socket, str(apdu[i:i+miu]))

        sent_apdu = apdu[2:]

        data = llc.recv(socket)
        size = struct.unpack(">H", data[0:2])[0]
        apdu = data[2:]
        while len(apdu) < size:
            data = llc.recv(socket)
            if data == None: break
            log.info("rcvd {0} byte data".format(len(data)))
            apdu += data
        info("rcvd {0} byte apdu".format(len(apdu)))

        rcvd_apdu = apdu[::-1]
        if rcvd_apdu != sent_apdu:
            raise TestError("received data does not equal sent data")

        llc.close(socket)
    
if __name__ == '__main__':
    TestProgram().run()
