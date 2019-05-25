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
	"player_name": "<LMS player name, can be overridden from command line>",

	"server": {
		"host": "<LMS host name>",
		"port": <port used for the LMS CLI interface>,
		"restart_delay": <delay in seconds before restart if connection is lost>
	},

	"default_script": "<default script, can be set to an empty string>",

	"events": {
		"power:on": [
			{
				"script": "<custom script, optional>",
				"param": "<script parameter, optional>",
				"include_value": <boolean flag to indicate if server response should be added as parameter>
			},
			{
				"delay": <delay in milliseconds before script execution, default 0>,
				"script": "<custom script, optional>",
				"param": "<script parameter, optional>",
			}
		],
		"power:off": [
			{
				"param": "<script parameter>",
			}
		],
		"volume:raise": [
			{
				"param": "<script parameter>",
			}
		],
		"volume:lower": [
			{
				"param": "<script parameter>",
			}
		]
	},
}
```
