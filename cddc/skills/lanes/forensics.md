Forensics. The flag is hidden in an artifact - a file, a capture, an image, a
memory/disk image, a log. Wide and flat: chase the strongest lead, don't grind a
weak one.

- Triage the artifact: `file`, `binwalk`, `exiftool`, `strings`, and the
  appropriate parser for its type.
- By type:
  - pcap: follow streams, extract transferred files/creds, decode protocols.
  - image/audio: stego sweep (LSB, appended data, spectrogram), metadata.
  - disk/memory: carve files, parse the filesystem / use volatility-style
    analysis.
  - office/pdf/archive: extract embedded objects, macros, hidden streams.
- Carve/extract the artifact, then read the flag out of it.

These are often guessy - if one lead is cold after a couple of tries, switch
leads rather than forcing it.
