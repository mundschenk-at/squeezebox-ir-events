# Squeezebox IR Events

A daemon sending IR commands based on Squeezebox events (written in µPython).

The implementation is heavily inspired by [ahebrank's script `watchpower.py`](https://github.com/ahebrank/squeezebox-utils).

## Usage

The script is intended to run as a daemon. It automatically restarts itself if the connection to the LMS server is lost. The first argument has to be the name of the configuration file, the second argument is optional, but recommended (the Squeezebox player name).

`micropython sb-ir-events.py <CONFIG_FILE> <PLAYER_NAME>`


## Configuration

The configuration file should be in JSON format:

```
{
	"IRSEND": "<path to irsend binary>",
	"REMOTE": "<name of LIRC remote>",
	"EVENTS": {
		"POWER_ON": [
			{
				"DELAY": <delay in milliseconds>,
				"CODE": "<LIRC button code 1>"
			},
			{
				"DELAY": <delay in milliseconds>,
				"CODE": "<LIRC button code 2>"
			}
		],
		"POWER_OFF": [
			{
				"DELAY": <delay in milliseconds>,
				"CODE": "<LIRC button code>"
			}
		],
		"VOLUME_RAISE": [
			{
				"DELAY": <delay in milliseconds>,
				"CODE": "<LIRC button code>"
			}
		],
		"VOLUME_LOWER": [
			{
				"DELAY": <delay in milliseconds>,
				"CODE": "<LIRC button code>"
			}
		]
	},
	"SERVER": {
		"HOST": "<LMS host name>",
		"PORT": <port used for the LMS CLI interface>,
		"RESTART_DELAY": <delay in seconds before restart if connection is lost>
	},
	"PLAYER_NAME": "<LMS player name, can be overridden from command line>"
}
```
