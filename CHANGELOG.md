# Change Log

## v1.0.0

Initial release.

The "Download" button is now enabled on SD card items. Clicking it will download the file to the OctoPrint uploads directory. If the file already exists, a confirmation dialog will appear asking if you want to overwrite the file. Because the transfer is performed using Base64, binary files are supported and do not require `BINARY_FILE_TRANSFER`.

Direct usage:

```
M6400 [R] [A] filename
Description: Dump the file specified to the serial terminal.

Parameters:
R   If passed, omit the B64_DATA prefix on each line, allowing text to be copied out of the terminal.
A   If passed, then the file will be dumped as plain-text instead of Base64-encoded. Do not run this on binary files.
```

```
M6401
Description: Recursively list all files on the SD card, including binary, hidden, and system files. Does not require BINARY_FILE_TRANSFER to work.
```

If SDCard long names are supported, then the file will be downloaded using its long name. Otherwise, it will be downloaded using its DOS 8.3 name.

## v0.1.0

Initial implementation.

