"""Read-only preflight for future official units; no exec or container changes."""
import argparse,datetime,json,os
import docker
p=argparse.ArgumentParser();p.add_argument('--cpu',required=True,type=int);a=p.parse_args()
client=docker.from_env(timeout=5)
try:
 container=client.containers.get('seer-icde-sqldriller-verieql')
 attrs=container.attrs;running=attrs['State']['Running'];pid=attrs['State']['Pid']
 host=sorted(os.sched_getaffinity(0));native=sorted(os.sched_getaffinity(pid)) if running else []
 # Query the kernel's effective mask instead of implementing a CPU-list parser.
 good=running and host==[a.cpu] and a.cpu in native
 result={'time':datetime.datetime.now().isoformat(),'state':'ready' if good else 'blocked_runtime_cpu_or_container',
         'expected_host_cpu':a.cpu,'actual_host_affinity':host,'container_running':running,
         'container_init_effective_affinity':native,'container_configured_cpuset':attrs['HostConfig']['CpusetCpus'],
         'container_name':container.name,'container_image_id':attrs['Image'],
         'scope':'read-only prelaunch runtime check; no SQL/model or Docker exec'}
 print(json.dumps(result,sort_keys=True),flush=True)
 raise SystemExit(0 if good else 2)
finally:client.close()
