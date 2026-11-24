# PC ↔ PLC Interface (Mitsubishi SLMP) – Proposal for the PLC Programmer

This document describes the signal interface implemented by MOBIS – Input Feeder
Flow Management. **No device addresses are fixed in the software** – every signal
below is configured on the *PLC (SLMP)* page and must be agreed with the PLC
programmer. Signals marked *optional* can be left empty.

> **Safety principle.** The PLC remains responsible for all physical outputs,
> hopper shutters and machine interlocks. The PC only sends validation results,
> loading authorisation and alarm requests. A successful PC write never
> guarantees that a physical operation is safe – the PLC program must apply its
> own interlocks.

## 1. Communication settings

| Setting | Options | Notes |
|---|---|---|
| Transport | TCP (recommended) / UDP | Must match the open setting of the PLC Ethernet port |
| Port | 1 – 65535 | As configured in GX Works (SLMP / MC protocol connection) |
| Frame | 3E / 4E | 4E adds a serial number to each request |
| Data code | Binary / ASCII | Must match the PLC "communication data code" |
| PLC series | Q / L, iQ-R, iQ-F (FX5) | iQ-R uses 4-byte device numbers; iQ-F uses octal X/Y numbers |
| Network No. / PC No. / module I/O / station | default 0 / 0xFF / 0x03FF / 0 | Own station; change only for routed access |
| Monitoring timer | default 16 × 250 ms = 4 s | PLC side response timeout |

Supported device types: `SM, SD, X, Y, M, L, F, V, B, D, W, TS, TC, TN, STS, STC,
STN, CS, CC, CN, SB, SW, DX, DY, R`. `X, Y, B, W, SB, SW, DX, DY` are numbered in
hexadecimal (X/Y in octal on iQ-F); all others in decimal. Bit devices are
read/written as single bits, word devices as unsigned 16-bit values.

## 2. Signal list

| # | Signal | Direction | Configuration field | Values (defaults, configurable) | Behaviour |
|---|---|---|---|---|---|
| 1 | Heartbeat | PC → PLC | `heartbeat_address` | toggles 0/1 (or counter 0…32767) every *heartbeat interval* | **PLC must** detect a frozen heartbeat (e.g. > 3 × interval) as "PC fault" and inhibit / close all shutters |
| 2 | PLC watchdog *(optional)* | PLC → PC | `plc_watchdog_address` | PLC increments continuously | PC treats the PLC as not ready when the value does not change within the timeout (PLC in STOP) |
| 3 | Alarm | PC → PLC | `alarm_address` | ON = 1, OFF = 0 | ON on every mismatch (wrong bag / colour / hopper / silo / tank / mold, duplicate or unknown barcode). PLC drives buzzer / tower light |
| 4 | Alarm reason code *(optional)* | PC → PLC | `alarm_code_address` | 0 none, 1 bag, 2 colour, 3 hopper, 4 silo, 5 distribution tank, 6 mold, 7 duplicate bag, 8 unknown barcode, 9 configuration, 10 unreadable bag barcode | For HMI display |
| 5 | Alarm acknowledgement *(optional)* | PLC → PC | `alarm_ack_address` | ack value = 1 (rising edge) | Used when *alarm reset* is "PLC acknowledgement" or "Operator or PLC" |
| 6 | Alarm reset command *(optional)* | PC → PLC | `alarm_reset_command_address` | pulse of 1 for 500 ms | Sent when the operator resets the alarm in the application |
| 7 | Validation result / authorisation | PC → PLC | `validation_result_address` (enable checkbox) | idle 0, OK 1, NG 2 | **OK = loading authorised** for the hopper in signal 8 |
| 8 | Authorised hopper code | PC → PLC | `hopper_code_address` | PLC code of the hopper (equipment master), 0 = none | PLC opens **only** this hopper's shutter |
| 9 | Destination code | PC → PLC | `destination_code_address` | PLC code of the silo / distribution tank, 0 = none | e.g. for diverter valves |
| 10 | Loading complete | PLC → PC | `loading_complete_address` | complete = 1 | **Latched** by the PLC after the shutter closed with light-curtain confirmation; reset by the PLC when the PC clears the authorisation |
| 11 | Shutter status *(optional)* | PLC → PC | `shutter_status_address` | open = 1 | Shown on screen; used in "shutter cycle" confirmation mode |
| 12 | Light curtain *(optional)* | PLC → PC | `light_curtain_address` | material detected = 1 | Shown on screen; required for "shutter cycle" mode if configured |
| 13 | Limit switch / ready *(per equipment, optional)* | PLC → PC | Equipment → `Limit switch / ready signal` | OK value per equipment | Checked before every authorisation ("IF LIMIT SWITCH OK" in the process flow chart) |

PLC codes (signals 8 and 9) are configured per equipment item. The initial
database uses placeholders (hoppers 1–4, silos 11–13, distribution tanks 21–24)
that must be confirmed.

## 3. Handshake sequence

```
            PC (MOBIS)                                   PLC
 idle       validation=0, hopper=0, destination=0
 NG scan    alarm=1, alarm_code=n, validation=2   ──►   alarm horn / light
 reset      alarm=0, alarm_code=0, validation=0   ──►   (optional reset pulse)
 all OK     hopper=<code>, destination=<code>,
            then validation=1  (in this order)     ──►   check own interlocks,
                                                         open shutter of <hopper>
                                                         operator fills material
                                                         light curtain detects flow
                                                         close shutter
            sees loading_complete rising edge      ◄──   loading_complete=1 (latched)
            records transaction + quantity
            validation=0, hopper=0, destination=0  ──►   reset loading_complete
```

* **Rising edge rule:** the PC accepts *loading complete* only after it has seen the
  signal inactive at least once since the authorisation – a signal left latched
  from the previous cycle can never complete a new transaction.
* **Cancel:** the PC may revoke the authorisation at any time (validation=0,
  codes=0). The PLC must then close the shutter in a safe way.
* **Communication loss:** the PC keeps a *desired output state* and re-writes all
  outputs (alarm and authorisation) after every reconnection. The PLC must handle
  the heartbeat timeout itself.
* **No new authorisation** is sent while the PLC link or heartbeat is faulty, the
  database is unavailable, an alarm is active, or a configured limit switch is
  not OK.
* **Application exit:** the PC revokes the authorisation before closing the
  connection; open transactions are cancelled (not completed).

## 4. Loading confirmation modes

| Mode | Completion when | Use |
|---|---|---|
| PLC loading-complete signal | Rising edge of signal 10 | Recommended once the PLC program is available |
| Shutter cycle | Signal 11 opens and closes again (with light curtain seen, if configured) | When only raw signals are exposed |
| Operator button | Operator presses LOADING COMPLETE | Commissioning / fallback without PLC signals |

A supervisor can force completion (audited) if the PLC confirmation is not
available in the PLC-driven modes.

## 5. Commissioning checklist

1. Configure the PLC Ethernet port (SLMP open setting, port, TCP/UDP, binary/ASCII).
2. Enter IP / port / frame / data code / series on the *PLC (SLMP)* page, keep
   *Use built-in PLC simulator* **off**, press **Test Connection** (reads the CPU type).
3. Enter the agreed device addresses; the page validates every address.
4. With the PLC programmer present and the machine safe, use *Manual Read / Write
   Test* to verify each signal.
5. Confirm the PLC codes of every hopper / silo / tank on the *Equipment* page.
6. Run a complete loading with a test bag and verify the transaction, the tank
   quantity and the PLC sequence.
