# Setup Log

## Environment (recorded Week 1)
```
CPU cores: 2
               total        used        free      shared  buff/cache   available
Mem:           883Mi       569Mi        68Mi        10Mi       388Mi       313Mi
Docker version 29.10.0, build fdd4b8e
Docker Compose version v5.8.0
Python 3.12.16
git version 2.52.0
```

## Environment (updated 2026-11-21)
- OS: RHEL 10.3 on Azure VM (Hyper-V), SELinux Enforcing, cgroup v2
- Resources: 2 vCPU, 3.6 GiB RAM, 2 GiB swap file
- Docker 29.10.0, Compose v5.8.0, Python 3.12.16, Git 2.52.0
- Cockpit disabled (port 9090 reserved for Prometheus)
- Monitoring UIs bound to 127.0.0.1; accessed via SSH tunnel from the laptop
