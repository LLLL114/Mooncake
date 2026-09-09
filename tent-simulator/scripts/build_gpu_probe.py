#!/usr/bin/env python3

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import pathlib,subprocess
ROOT=pathlib.Path(__file__).resolve().parents[1];b=OUTPUT_ROOT/'build/release';s=(ROOT/'simulator/cm_probe.cpp').read_text()
s='#include <cuda.h>\n#include <vector>\n'+s
s=s.replace('int rc=1;','int rc=1;CUcontext gpu_context=nullptr;CUdeviceptr gpu_pointer=0;bool gpu=false;')
s=s.replace('need(argc==5,"usage: cm_probe server|client destination_ip port source_ip");','need(argc==5 || argc==6,"usage: cm_probe server|client destination_ip port source_ip [gpu]");')
s=s.replace('constexpr size_t bytes=65536;need(!posix_memalign(&payload,4096,bytes),"allocate");memset(payload,server?0:0x5a,bytes);','''constexpr size_t bytes=65536;gpu=server && argc==6;
  if(gpu) {CUdevice device;need(cuInit(0)==CUDA_SUCCESS,"cuInit");need(cuDeviceGet(&device,atoi(argv[5]))==CUDA_SUCCESS,"CUDA device");need(cuCtxCreate(&gpu_context,0,device)==CUDA_SUCCESS,"CUDA context");need(cuMemAlloc(&gpu_pointer,bytes)==CUDA_SUCCESS,"CUDA allocate");need(cuMemsetD8(gpu_pointer,0,bytes)==CUDA_SUCCESS,"CUDA memset");need(cuCtxSynchronize()==CUDA_SUCCESS,"CUDA sync");payload=reinterpret_cast<void*>(gpu_pointer);}
  else {need(!posix_memalign(&payload,4096,bytes),"allocate");memset(payload,server?0:0x5a,bytes);}''')
s=s.replace('need(notify==0x77,"notify value");for(size_t i=0;i<bytes;++i)need(static_cast<unsigned char*>(payload)[i]==0x5a,"payload mismatch");','''need(notify==0x77,"notify value");std::vector<unsigned char> host(bytes);const unsigned char* checked=static_cast<unsigned char*>(payload);
   if(gpu){need(cuMemcpyDtoH(host.data(),gpu_pointer,bytes)==CUDA_SUCCESS,"CUDA readback");checked=host.data();}
   for(size_t i=0;i<bytes;++i)need(checked[i]==0x5a,"payload mismatch");''')
s=s.replace('free(payload);return rc;','if(gpu_pointer)cuMemFree(gpu_pointer);else free(payload);if(gpu_context)cuCtxDestroy(gpu_context);return rc;')
assert 'CUDA readback' in s and 'CUDA allocate' in s
(b/'cm_gpu.cpp').write_text(s)
subprocess.run(['g++','-O2','-I/usr/local/cuda/include',str(b/'cm_gpu.cpp'),'-lrdmacm','-libverbs','-L/usr/local/cuda/lib64/stubs','-lcuda','-o',str(b/'cm_gpu')],check=True)
print('GPU_BUILD_OK')
