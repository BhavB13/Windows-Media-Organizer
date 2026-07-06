# Application Architecture

Phase 1 separates long-running operations from frontend widgets while retaining
the tested legacy engine and its JSON cache and journal formats.

## Dependency direction

```text
Frontend
  -> Qt controllers
    -> framework-neutral services
      -> existing discovery, engine, ADB, cache, and safety modules
```

Core contracts and services never import a frontend framework. Controllers are
the only layer that imports Qt. The temporary Tkinter frontend invokes the same
services from Qt-owned workers and schedules all widget updates on Tk’s main
thread.

## Core contracts

- `OperationEvent` carries phase, state, progress, byte and item counts, current
  item, rate, ETA, severity, message, timestamp, and structured details.
- `OperationResult` carries terminal status, counts, duration, warnings,
  failures, report path, resume information, and operation-specific data.
- `CancellationToken` provides cooperative cancellation and remains compatible
  with engine functions expecting `threading.Event.is_set()`.
- `StructuredError` separates safe user-facing guidance from technical details
  retained by the activity log.
- `AppSettings` contains appearance, simple/advanced mode, transfer defaults,
  diagnostic consent, update channel, and Android preferences.
- `QuarantineRecord` describes the original and stored paths, content hash,
  size, reason, operation ID, and timestamp.

## Operation lifecycle

Controllers permit one operation at a time and expose Qt signals for progress,
state changes, recoverable errors, completion, cancellation, failure, technical
logs, and busy-state changes.

Supported states are:

```text
idle -> validating -> scanning/comparing/transferring
                    -> reconnecting/paused
                    -> cancelling
                    -> completed/cancelled/failed
```

`DuplicateScanController` and `TransferController` execute the primary
operations. Device, report, quarantine, settings, diagnostics, and update
controllers use the same worker and error boundary.

## Threading rules

- Widget values must be captured before work starts.
- Scanning, hashing, ADB commands, transfers, and file-backed controller tasks
  execute through `QThreadPool`.
- Workers emit data objects only; they never read or mutate widgets.
- Qt automatically queues controller signal delivery to the controller’s main
  thread.
- The legacy Tk adapter uses `after()` for every worker-originated widget
  update.

## Compatibility

Phase 1 intentionally preserves:

- Runtime hash-cache JSON
- Drive and ADB cache JSON version 1
- Transfer journal JSON version 1
- Existing transfer report JSON
- Root engine APIs and their legacy callback signatures

The services adapt those callbacks into structured events so later phases can
replace the frontend without rewriting the transfer engine.
