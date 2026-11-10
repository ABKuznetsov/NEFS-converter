# SEM Data Converter

Standalone desktop utility for converting SEM measurement datasets to binary NESF.

Based on code from [aztec-archive-recovery](https://github.com/nuntiatioone/aztec-archive-recovery),
revision `01f599657e693181f6d1a22e7c8c1461c2082bd9`, with an adapted data reader,
NESF writer, validation and a Tkinter interface. See [third-party notices](THIRD_PARTY_NOTICES.md).

## Run

Requires Python 3.11+, Windows x64, Windows PowerShell 5.1 and a separately
installed Microsoft SQL Server Compact 4 Desktop x64 runtime. No additional
Python packages are required.

```powershell
python -m sem_data_converter
```

Alternatively, open `SEM Data Converter.pyw`.
Select the SEM file and the NESF output filename, then click **Create NESF**.
The NESF stays beside the original `data/` directory. DAT files are not
embedded, copied or modified. Existing output is replaced only after confirmation.

Command-line conversion and validation are also available:

```powershell
python -m sem_data_converter convert "<SEM-file>"
python -m sem_data_converter validate "<project.nesf>"
```

This is a demonstration build for supported source layouts, not a universal
reader. Unknown metadata is reported rather than replaced with zero values.
Physical calibration interpretations require independent validation.

## License

MIT. The original upstream license is included locally under `licenses/`,
so attribution and license terms do not depend on the availability of an external link.
No measurement datasets or third-party runtime binaries are distributed here.
