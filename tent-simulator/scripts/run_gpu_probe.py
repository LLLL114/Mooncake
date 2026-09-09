#!/usr/bin/env python3

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import pathlib,subprocess,time,json,hashlib
ROOT=pathlib.Path(__file__).resolve().parents[1];out=OUTPUT_ROOT/'runs/experiment-ef/gpu';out.mkdir(parents=True,exist_ok=True);binary=OUTPUT_ROOT/'build/release/cm_gpu';rows=[]
for gpu in range(8):
 for rail in range(2):
  ip='10.0.1.244' if rail else '10.0.1.243';port=str(18700+gpu*2+rail);log=out/f'g{gpu}-r{rail}-server.log'
  with log.open('w') as f:
   server=subprocess.Popen([str(binary),'server',ip,port,ip,str(gpu)],stdout=f,stderr=subprocess.STDOUT)
   until=time.monotonic()+5
   while time.monotonic()<until and server.poll() is None and 'LISTEN_READY' not in log.read_text():time.sleep(.05)
   client_code=None;client_log='no listener';expired=False
   if 'LISTEN_READY' in log.read_text():
    try:r=subprocess.run([str(binary),'client',ip,port,ip],capture_output=True,text=True,timeout=15);client_code=r.returncode;client_log=r.stdout+r.stderr
    except subprocess.TimeoutExpired as e:expired=True;client_log=str(e)
   try:server.wait(timeout=3)
   except subprocess.TimeoutExpired:server.terminate();server.wait(timeout=3);expired=True
  (out/f'g{gpu}-r{rail}-client.log').write_text(client_log)
  row={'gpu':gpu,'rail':rail,'server_rc':server.returncode,'client_rc':client_code,'watchdog':expired,'server_log':log.read_text(),'client_log':client_log,'payload_verified':server.returncode==0 and client_code==0 and 'PAYLOAD_PASS' in log.read_text()};rows.append(row)
  print('GPU_PAIR',gpu,rail,row['payload_verified'],flush=True)
(out/'summary.json').write_text(json.dumps({'pairs':rows,'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'scope':'64KiB host-to-GPU RDMA WRITE and CUDA readback; no GPU-to-host throughput or sustained GDR stability'},indent=2)+'\n')
