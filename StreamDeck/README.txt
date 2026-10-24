DA Timer - Stream Deck
======================

There are two ways to use a Stream Deck with DA Timer. Pick ONE.

A) DIRECT (recommended) - no Elgato software needed
  1. Quit the Elgato Stream Deck app (right-click its tray icon > Quit),
     or uninstall it / remove it from startup on the show PC.
  2. Plug the Stream Deck in and start DA Timer. It's picked up
     automatically (and again if you unplug / replug it).
  3. Click DECK on DA Timer's toolbar to choose what each key does,
     set brightness, or turn direct control off.
  Works with Stream Deck MK.2 / Original, Mini, XL, Neo, Plus, Plus XL,
  Studio (keys). Needs hidapi.dll, da_deck.py and the vendor folder next
  to da_timer.py (the .exe from "Build EXE.bat" has them built in).

B) ELGATO PLUGIN - if you want to keep using the Stream Deck app
  Double-click "DA Timer.streamDeckPlugin", then drag actions from the
  "DA Timer" category. In DA Timer, click DECK and untick "Control the
  Stream Deck directly" so the two don't fight over the keys.

Key actions (both ways)
  Live Clock           Segment / program time, segment or total over/under,
                       or TOD. Press: start/stop that timer, next segment,
                       MARK, or nothing.
  All Start / Stop     Toggle both timers (RUNNING / STOPPED / MIXED)
  All Start, All Stop  Separate keys, lit green / red like the app
  Segment Start/Stop   With live segment time and over/under
  Program Start/Stop   With live program time and total over/under
  Next Segment         Shows the next segment number and name
  Log Mark             Timestamped MARK in the log
  Reset Program        HOLD 1 second (a tap does nothing - safety)
  Display Toggle       Output to monitor / TOD / log panel / segments panel
  Undo Last Action     Undo a reset, next segment, stop/start or preset

Other controllers (Bitfocus Companion, generic HTTP)
  GET  http://127.0.0.1:47800/api/state
  POST http://127.0.0.1:47800/api/action/<command>   header  X-DA-Timer: 1
  Commands: all_start all_stop all_toggle seg_start seg_stop seg_toggle
            prog_start prog_stop prog_toggle next_segment mark reset_program
            dual_toggle tod_toggle log_toggle segments_toggle undo
