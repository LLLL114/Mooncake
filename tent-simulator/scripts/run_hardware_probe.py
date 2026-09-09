#!/usr/bin/env python3
"""Bounded same-host CM trials; no NIC/link changes and no external endpoint."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import json,pathlib,subprocess,time,hashlib
ROOT=pathlib.Path(__file__).resolve().parents[1]
def main():
 out=OUTPUT_ROOT/'runs/experiment-ef/hardware';out.mkdir(parents=True,exist_ok=True);rows=[]
 binary=OUTPUT_ROOT/'build/release/cm_probe'
 for i,(source,dest) in enumerate([('10.0.1.243','10.0.1.243'),('10.0.1.244','10.0.1.244'),('10.0.1.243','10.0.1.244'),('10.0.1.244','10.0.1.243')]):
  port=str(18551+i);server_cmd=[str(binary),'server',dest,port,dest];client_cmd=[str(binary),'client',dest,port,source]
  log=out/f'pair-{i}-server.log'
  with log.open('w') as f:
   server=subprocess.Popen(server_cmd,stdout=f,stderr=subprocess.STDOUT)
   deadline=time.monotonic()+5
   while time.monotonic()<deadline and server.poll() is None and 'LISTEN_READY' not in log.read_text():time.sleep(.05)
   client_code=None;client_log='not attempted: listener did not become ready';timeout=False
   if 'LISTEN_READY' in log.read_text():
    try:r=subprocess.run(client_cmd,capture_output=True,text=True,timeout=12);client_code=r.returncode;client_log=r.stdout+r.stderr
    except subprocess.TimeoutExpired as e:client_log=str(e);timeout=True
   try:server.wait(timeout=3)
   except subprocess.TimeoutExpired:server.terminate();server.wait(timeout=3);timeout=True
  (out/f'pair-{i}-client.log').write_text(client_log)
  row={'source':source,'destination':dest,'server_command':server_cmd,'client_command':client_cmd,'server_returncode':server.returncode,'client_returncode':client_code,'watchdog_triggered':timeout,'server_log':log.read_text(),'client_log':client_log,'payload_verified':server.returncode==0 and client_code==0 and 'PAYLOAD_PASS' in log.read_text()}
  rows.append(row);print('CM_PAIR',i,row['server_returncode'],client_code,row['payload_verified'],flush=True)
 (out/'summary.json').write_text(json.dumps({'pairs':rows,'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'scope':'same-host DRAM RDMA WRITE capability, not throughput calibration or full TENT backend'},indent=2)+'\n')
if __name__=='__main__':main()
