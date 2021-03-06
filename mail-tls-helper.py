#!/usr/bin/python

# Postfix helper script that does the following:
#  * make TLS mandatory for outgoing mail wherever possible and
#  * alert postmasters of mailservers that don't support STARTTLS
#
# Author: doobry@systemli.org
# Version: 0.7 [2017-02-19]
# License: GPL-3
#
# TODO:
# * split things out into submodules: mail sending, postfix
#   * maybe not? complicates installation of the script
# * implement blacklist of domains/relays not to notify when no-tls (?)
# * writer log parser and hash map creator for exim4
# * restructure code into clases: options, sqlite, postfix, (exim4), sendmail
# * make more things configurable via commandline:
#   * interval between mails to postmasters

from __future__ import print_function
import os
import re
import datetime, sqlite3
import getopt, sys
from collections import defaultdict
from subprocess import call
from subprocess import Popen, PIPE
import smtplib
from email.MIMEMultipart import MIMEMultipart
from email.MIMEText import MIMEText
from email.Utils import COMMASPACE, formatdate

name = "mail-tls-helper.py"
version = "0.7"

global op, notlsDomains, notlsRelays, tlsDomains, tlsRelays
global conCount, lineCount, msgCount, sentCount, tlsCount
op = {}
tlsRelays = set()
tlsDomains = set()
notlsRelays = set()
notlsDomains = set()
conCount = lineCount = msgCount = sentCount = tlsCount = 0

# Structure for pidDict
def relayFactory():
    return {
        'msgIds': {},
        'domains': set(),
        'msgCount': 0,
        'conCount': 0,
        'sentCount': 0,
        'tlsCount': 0,
    }

def pidFactory():
    return defaultdict(relayFactory)

pidDict = defaultdict(pidFactory)

# Parse options
def options(args):
    op['printHelp'] = False
    op['printVersion'] = False

    try:
        opts, args = getopt.getopt(args, 'Ad:f:hl:m:Op:Pr:s:SV',
            ['no-alerts', 'domain=', 'debug', 'from=', 'help',
             'mail-log=', 'mode=', 'no-postmap', 'postfix-map-file=',
             'no-postfix-map', 'rcpts=', 'sqlite-db=', 'no-summary',
             'version'])
    except getopt.error as exc:
        print("%s: %s, try -h for a list of all the options" % (name, str(exc)), file=sys.stderr)
        sys.exit(255)

    for opt, arg in opts:
        if opt in ['-h', '--help']:
            op['printHelp'] = True
            break
        elif opt in ['-V', '--version']:
            op['printVersion'] = True
            break
        elif opt in ['-m', '--mode']:
            if (arg == 'postfix'):
                op['mode'] = arg
            else:
                print("%s: unknon mode %s, try -h for a list of all the options" % (name, arg), file=sys.stderr)
                sys.exit(255)
        elif opt in ['--debug']:
            op['debug'] = True
        elif opt in ['-l', '--mail-log']:
            op['mailLog'] = arg
        elif opt in ['-P', '--no-postfix-map']:
            op['postfixMap'] = False
        elif opt in ['-p', '--postfix-map-file']:
            op['postfixMapFile'] = arg
        elif opt in ['-O', '--no-postmap']:
            op['postMap'] = False
        elif opt in ['-s', '--sqlite-db']:
            op['sqliteDB'] = arg
        elif opt in ['-A', '--no-alerts']:
            op['alerts'] = False
        elif opt in ['-S', '--no-summary']:
            op['summary'] = False
        elif opt in ['-d', '--domain']:
            op['domain'] = arg
        elif opt in ['-f', '--from']:
            op['from'] = arg
        elif opt in ['-r', '--rcpts']:
            op['rcpts'] = arg.split(',')

    # Set options to defaults if not set yet
    op['debug']      = op.get('debug', False)
    op['mode']       = op.get('mode', "mode")
    op['mailLog']    = op.get('mailLog', "/var/log/mail.log.1")
    op['postfixMap'] = op.get('postfixMap', True)
    op['postfixMapFile'] = op.get('postfixMapFile', "/etc/postfix/tls_policy")
    op['postMap']    = op.get('postMap', True)
    op['sqliteDB']   = op.get('sqliteDB', "/var/lib/mail-tls-helper/notls.sqlite")
    op['alerts']     = op.get('alerts', True)
    op['summary']    = op.get('summary', True)
    op['domain']     = op.get('domain', "example.org")
    op['from']       = op.get('from', "admin@%s" % op['domain'])
    op['rcpts']      = op.get('rcpts', [ "admin@%s" % op['domain'] ])
    op['summSubj']  = op.get('sumSubj', "[%s] mail-tls-helper summary" % (os.uname()[1]))
    op['summBody']  = op.get('sumSubj', "Summary mail by mail-tls-helper on %s" % (os.uname()[1]))
    op['alertSubj'] = op.get('alertSubj', "Please add TLS support to the mailservers for 'XDOMAINX'")
    op['alertBody'] = op.get('alertBody', """Hello postmaster for mail domain 'XDOMAINX',

Your mail server for 'XDOMAINX' is among the last mail servers,
that still don't support TLS transport encryption for incoming messages.


In order to make the internet a safer place, we intend to disable
unencrypted mail delivery in the near future.

Please do your users a favour, join our effort and add STARTTLS support
to your mail setup.

See RFC 3207 for further information: https://tools.ietf.org/html/rfc3207

In case of any questions, don't hesitate to contact us at
%s

Kind regards,
%s sysadmins
""" % (op['from'], op['domain']))

    if op['printHelp']:
        print("usage: %s [options]" % name, file=sys.stderr)
        print("""
Postfix helper script that does the following:
 * make TLS mandatory for outgoing mail wherever possible and
 * alert postmasters of mailservers that don't support STARTTLS

%s options:
  -h, --help                   display this help message
  -V, --version                display version number
      --debug                  run in debugging mode, don't do anything
  -m, --mode=[postfix]         set mode (currently only postfix is supported)
  -l, --mail-log=file          set mail log file (default: %s)
  -P, --no-postfix-map         don't update the Postfix TLS policy map file
  -p, --postfix-map-file=file  set Postfix TLS policy map file (default: %s)
  -O, --no-postmap             don't postmap(1) the Postfix TLS policy map file
  -s, --sqlite-db=file         set SQLite DB file (default: %s)
  -A, --no-alerts              don't send out alert mails
  -S, --no-summary             don't send out summary mail
  -d, --domain=name            set organization domain (default: %s)
  -f, --from=address           set sender address (default: %s)
  -r, --rcpts=addressses       set summary mail rcpt addresses (default: %s)
""" % (name, op['mailLog'], op['postfixMapFile'], op['sqliteDB'], op['domain'], op['from'], ','.join(op['rcpts'])), file=sys.stderr)
        sys.exit(0)
    elif op['printVersion']:
        print("%s %s" % (name, version), file=sys.stderr)
        sys.exit(0)

# Print debugging messages
def print_dbg(msg):
    if op['debug']:
        print("DEBUG: %s" % msg)

def print_dbg_pid(pid):
    print_dbg("PID: %s" % pid)
    for relay in pidDict[pid]:
        print_dbg_relay(relay)

def print_dbg_relay(relay):
    print_dbg(" relay: %s" % relay)
    print_dbg("  msgIds: %s" % pidDict[pid][relay]['msgIds'])
    print_dbg("  domains: %s" % pidDict[pid][relay]['domains'])
    print_dbg("  msgCount: %s" % pidDict[pid][relay]['msgCount'])
    print_dbg("  conCount: %s" % pidDict[pid][relay]['conCount'])
    print_dbg("  tlsCount: %s" % pidDict[pid][relay]['tlsCount'])
    print_dbg("  sentCount: %s" % pidDict[pid][relay]['sentCount'])

# Postfix TLS policy table functions
def postfixTlsPolicyRead():
    if os.path.isfile(op['postfixMapFile']):
        #return [line.split()[0].strip('[]') for line in open(op['postfixMapFile'])]
        return [line.split()[0] for line in open(op['postfixMapFile'])]
    else:
        return []

def postfixTlxPolicyWrite(policyFileLines):
    fmode = "r" if op['debug'] else "w"
    policyFile = open(op['postfixMapFile'], "a")
    for domain in tlsDomains:
        if domain not in policyFileLines:
            print_dbg("Add domain '%s' to Postfix TLS policy map" % domain)
            #policyFile.write("[%s] encrypt\n" % domain)
            if not op['debug']: policyFile.write("%s encrypt\n" % domain)
    policyFile.close()

def postmapTlsPolicy():
    if op['postMap'] and not op['debug']:
        call(["postmap", op['postfixMapFile']])

def sqliteDBRead():
    notlsDict = {}
    if os.path.isfile(op['sqliteDB']):
        conn = sqlite3.connect(op['sqliteDB'])
        c = conn.cursor()
        c.execute("SELECT * FROM notlsDomains")
        rows = c.fetchall()
        conn.close()
        for item in rows:
            notlsDict[item[0]] = {
                'alertCount': item[1],
                'alertDate': item[2],
            }
    return notlsDict

def notlsProcess(notlsDict):
    op['summBody'] += "\nList of domains with no-TLS connections:"
    conn = sqlite3.connect(op['sqliteDB'])
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS notlsDomains (domain text, alertCount integer, alertDate date)")
    for domain in notlsDomains:
        if domain in tlsDomains:
            # ignore individual no-TLS connections when other connections
            # to the same server were encrypted. TLS to the domain will be
            # mandatory in the future anyway.
            continue
        op['summBody'] += "\n * %s" % (domain)
        if domain in notlsDict:
            # send alerts every 30 days
            slist = notlsDict[domain]['alertDate'].split('-')
            if not datetime.date(int(slist[0]),int(slist[1]),int(slist[2])) < datetime.date.today()+datetime.timedelta(-30):
                continue
            else:
                print_dbg("Update domain %s in sqlite DB" % domain)
                if not op['debug']: c.execute("UPDATE notlsDomains SET alertCount=?, alertDate=? WHERE domain=?", (notlsDict[domain]['alertCount']+1, datetime.date.today(), domain))
        else:
            print_dbg("Insert domain %s into sqlite DB" % domain)
            if not op['debug']: c.execute("INSERT INTO notlsDomains (domain, alertCount, alertDate) VALUES (?,?,?)", (domain, 1, datetime.date.today()))
        if op['alerts']:
            op['summBody'] += " [sent alert mail]"
            sendMail(['postmaster@'+domain],
                     op['alertSubj'].replace('XDOMAINX', domain),
                     op['alertBody'].replace('XDOMAINX', domain))
    op['summBody'] += "\n\n"
    c.execute
    conn.commit()
    conn.close()

# Send mail
def sendMail(to, subject, text, server="/usr/sbin/sendmail"):
    assert type(to)==list
    msg = MIMEMultipart()
    msg['From'] = op['from']
    msg['To'] = COMMASPACE.join(to)
    msg['Date'] = formatdate(localtime=True)
    msg['Subject'] = subject
    msg.attach(MIMEText(text))
    if op['debug']:
        print_dbg("Mail: %s" % msg.as_string())
    else:
        if server == "/usr/sbin/sendmail":
            p = Popen([server, "-t", "-oi"], stdin=PIPE)
            p.communicate(msg.as_string())
        else:
            smtp = smtplib.SMTP(server)
            smtp.sendmail(op['from'], to, msg.as_string())
            smtp.close()

# Regexes
regex_postfix_smtp = re.compile(r" postfix/smtp\[(?P<pid>[0-9]+)\]: (?P<msgid>[0-9A-F]+): to=<[^@]+@(?P<domain>[^, ]+)>, .*relay=(?P<relay>[\w\-\.]+)\[[0-9A-Fa-f\.:]+\]:[0-9]{1,5}, .*status=(?P<status>[a-z]+)")
regex_postfix_conn_err = re.compile(r" postfix/smtp\[(?P<pid>[0-9]+)\]: (?P<msgid>[0-9A-F]+): (conversation withhost|lost connection with) (?P<relay>[\w\-\.]+)\[[0-9A-Fa-f\.:]+\](:[0-9]{1,5})? ")
regex_postfix_tls  = re.compile(r" postfix/smtp\[(?P<pid>[0-9]+)\]: .*TLS connection established to (?P<relay>[\w\-\.]+)\[[0-9A-Fa-f\.:]+\]:[0-9]{1,5}")
# Untested:
regex_exim4_smtp = re.compile(r"(?P<msgid>[\w\-]{14}) [=-]> .*T=remote_smtp .*H=(?P<relay>[\w\-\.]+) .*(X=(?P<tlsver>[A-Z0-9\.]+):[\w\-\.:_]+)? .*C=\"(?P<response>[^\"]+)\"")
regex_exim4_comp = re.compile(r"(?P<msgid>[\w\-]{14}) Completed")

# Main function
if __name__ == '__main__':
    options(sys.argv[1:])

    # Read SMTP client connections from Postfix logfile into pidDict
    # * SMTP client connection logs don't contain TLS evidence. Thus
    #   TLS connections logs have to be parsed alongside.
    with open(op['mailLog'], "r") as logFile:
        for line in logFile:
            lineCount += 1
            # search for SMTP client connections
            m = regex_postfix_smtp.search(line)
            if m:
                conCount += 1
                relay = m.group('relay').lower()
                domain = m.group('domain').lower()
                pidDict[m.group('pid')][relay]['domains'].add(domain)
                pidDict[m.group('pid')][relay]['conCount'] += 1
                if m.group('status') == 'sent':
                    pidDict[m.group('pid')][relay]['sentCount'] += 1
                    sentCount += 1
                if not m.group('msgid') in pidDict[m.group('pid')][relay]['msgIds'].keys():
                    pidDict[m.group('pid')][relay]['msgCount'] += 1
                    msgCount += 1
                pidDict[m.group('pid')][relay]['msgIds'][m.group('msgid')] = m.group('status')
                continue
            # search for connection errors
            m = regex_postfix_conn_err.search(line)
            if m:
                relay = m.group('relay').lower()
                conCount += 1
                pidDict[m.group('pid')][relay]['conCount'] += 1
                if not m.group('msgid') in pidDict[m.group('pid')][relay]['msgIds'].keys():
                    pidDict[m.group('pid')][relay]['msgCount'] += 1
                    msgCount += 1
            # search for TLS connections
            m = regex_postfix_tls.search(line)
            if m:
                relay = m.group('relay').lower()
                pidDict[m.group('pid')][relay]['tlsCount'] += 1
                tlsCount += 1

    print_dbg("Processed lines: %s" % lineCount)
    print_dbg("Total connections: %s" % conCount)
    print_dbg("Total messages: %s" % msgCount)
    print_dbg("Delivered messages: %s" % sentCount)
    print_dbg("TLS connections: %s" % tlsCount)

    # Process pidDict, read relays into tlsRelays/notlsRelays and domains into
    # tlsDomains/notlsDomains
    # * Beware:
    #   * Postfix sends several mails - even to different relays - under one
    #     PID, each one with a separate msgID.
    #   * Several connections may exist per msgID (e.g. if first attempt to
    #     send fails).
    #   * One TLS connection may be used to send several mails to one relay.
    relayConnCount = relayTLSCount = 0
    for pid in pidDict:
        #print_dbg_pid(pid)
        for relay in pidDict[pid]:
            relayConnCount += 1
            if (pidDict[pid][relay]['tlsCount'] > 0 and
                pidDict[pid][relay]['sentCount'] > 0):
                # At least one encrypted connection and one delivered message
                relayTLSCount += 1
                tlsRelays.add(relay)
                for domain in pidDict[pid][relay]['domains']:
                    tlsDomains.add(domain)
            elif (pidDict[pid][relay]['tlsCount'] > 0):
                # No message got delivered, still encrypted connection: ignore
                relayTLSCount += 1
            else:
                # Only unencrypted connections
                notlsRelays.add(relay)
                for domain in pidDict[pid][relay]['domains']:
                    notlsDomains.add(domain)

    op['summBody'] += "\n\nTotal separate server connections: %s\n" % relayConnCount
    op['summBody'] += "noTLS separate server connections(abs): %s\n" % (relayConnCount-relayTLSCount)
    op['summBody'] += "noTLS separate server connections(rel): %.2f%%\n" % ((relayConnCount-relayTLSCount)/float(relayConnCount)*100)

    if (len(tlsDomains) > 0 and op['postfixMap']):
        policyFileLines = postfixTlsPolicyRead()
        postfixTlxPolicyWrite(policyFileLines)
        postmapTlsPolicy()

    if len(notlsDomains) > 0:
        notlsDict = sqliteDBRead()
        notlsProcess(notlsDict)
        if op['summary']:
            sendMail(op['rcpts'],op['summSubj'],op['summBody'])
