# DawdleRPG

DawdleRPG is an IdleRPG clone written in Python. This fork is for ubuntu it is almost 98% finished.

## Basic Setup new

sudo apt install python3 python3-pip git nano -y

git clone https://github.com/chandro/dawdlerpg.git

cd dawdlerpg

chmod +x install.sh

sudo ./install.sh host.name

if you get an error on the last setp of install.sh run it manually:

/usr/bin/python3 /root/dawdlerpg/dawdle.py -o daemonize=off /root/dawdlerpg/data/dawdle.conf



## Basic Setup (old)
- Edit `dawdle.conf` to configure your bot. remember to use SSL port!
- Run `dawdle.py <path to dawdle.conf>`
- The data directory defaults to the parent directory of the
  configuration file, and dawdlerpg expects files to be in that
  directory.

## Setup with Website

The included `install.sh` script will set up the dawdlerpg bot and
website on a freshly installed Debian system.  It uses nginx, uwsgi,
and django for the site.  At some point, you should be prompted to
edit the dawdle.conf file, and you'll need to edit some configuration
parameters explained by the comments in the file.

```sh
./install.sh <hostname>
```

If you don't have a clean install, you should instead look at the
`install.sh` script and use the pieces that work for your setup.

## Migrating from IdleRPG

DawdleRPG is capable of being a drop-in replacement.

- Run `dawdle.py <path to old irpg.conf>`

If you have any command line overrides to the configuration, you will
need to replace them with the `-o key=value` option.

## Differences from IdleRPG

- Names, items, and durations are in different colors.
- Output throttling allows configurable rate over a period.
- Long messages are word wrapped.
- Logging can be set to different levels.
- Better IRC protocol support.
- More game numbers are configurable.
- Quest pathfinding is much more efficient.
- Fights caused by map collisions have chance of finding item.
- All worn items have a chance to get buffs/debuffs instead of a subset.
- High level character can still get special items.
- Special items are always buffs.
