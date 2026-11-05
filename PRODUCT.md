# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Existing codebase: Chrome extension (Manifest V3, vanilla HTML/CSS/JS, no framework) in `chrome-extension/`, reading a local Python bridge in `bridge/` over loopback with a token. Kept as is.

## Users

One person: the owner of the Oraimo Watch 6R (Eyad), using it on a Windows PC. He opens many new tabs a day and wants a glance, not a session. Job: see how today is going (steps against goal, heart rate), whether the watch is connected and charged, and how recent days compare.

## Product Purpose

A read-only daily dashboard for the Oraimo Watch 6R (MOYOUNG-V2) that replaces Chrome's New Tab page. Success is that one glance at a new tab answers: how many steps against the goal, what is my heart rate, is the watch connected and charged, and how did yesterday go. No account, no cloud, no writes to the watch.

## Positioning

Local-first and honest: data comes straight from the watch through a bridge on the user's own machine, and anything the watch has not reported is shown as unavailable, never as zero or a guess.

## Operating Context

The watch talks BLE to the PC through the bridge, and the watch accepts one connection at a time (the phone's Bluetooth must be off). Data arrives unevenly: heart rate only while the watch is measuring, live activity only when the watch pushes a notification, history and device info on load and on a slow poll. The bridge may be stopped, starting, or reconnecting, and the dashboard must say so plainly.

## Capabilities and Constraints

- Shown today: steps, distance, calories (live values are provisional), daily step goal (8000 on this watch), battery, live heart rate, heart-rate history in 5-minute samples grouped into 6-hour blocks, hourly activity (bucket meaning provisional), daily activity for yesterday and the day before (steps first, then distance and calories; confirmed against the watch), sleep only when recorded, device info. The dashboard can explicitly fetch the current activity characteristic through the existing local BLE connection; this is a read, not historical activity.
- Steps, distance and calories from the history record are confirmed. The hourly steps mapping and the live FEE1 interpretation are provisional and are labelled as such.
- The watch has returned no sleep records so far. Sleep is never invented.
- Diagnostics (connection events, raw notifications with unknown bytes preserved verbatim) stay available but secondary.
- Undecided: whether provisional fields will be promoted once confirmed against the watch.

## Brand Commitments

Volunteered by the user and binding: a bento-grid layout, designed as a daily dashboard for the browser new tab, and it must not look generic or "vibecoded".

## Evidence on Hand

Real captures from the watch (probe and bridge runs), including yesterday's 6,801 steps and a full set of heart-rate history blocks. No sleep, alarm or workout data exists. Do not fabricate any.

## Product Principles

1. Unavailable is not zero: missing data reads as missing, in every state.
2. Glance first, depth on demand: today's steps against goal leads, then heart rate, then battery and connection, then recent days. Diagnostics sit behind a deliberate action.
3. Quiet when healthy, clear when broken: a connected watch shows no chrome about connecting; a problem names its cause and next step.
4. Read-only and local: nothing here writes to the watch or leaves the machine.
