# OctoPrint-M6400Download

This plugin implements the M6400 Base64 download feature.

The transport layer queues `M6400 <filename>` and buffers Marlin's
`B64_DATA` base64 records until it receives `B64_END`, then decodes and saves
the file in OctoPrint's local uploads storage. `request_download(filename)`
refuses an existing local file by default; pass `force=True` to overwrite it.

## Setup

Install via the bundled [Plugin Manager](https://docs.octoprint.org/en/main/bundledplugins/pluginmanager.html)
or manually using this URL:

    https://github.com/kaedenn/OctoPrint-M6400Download/archive/main.zip

This plugin requires a custom Marlin compilation with M6400 and M6401, which is implemented via [Kaedenn's Marlin repository](https://github.com/Kaedenn/Ender3-Marlin.git).
