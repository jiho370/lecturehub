# Web trial (GitHub Pages)

The real app screen (ui/web, from the PUBLIC export) in a browser, answering bridge calls from recordings of the
real app on the synthetic trial semester, plus playback of recording / live transcript / note making / study-room
booking / key and eTL connection (bridge.js). Nothing leaves the browser.

1. Make a trial semester with scratch settings (LECTUREHUB_SETTINGS and LECTUREHUB_RECORDER_CONFIG in a temp folder):
   python -m lecturehub.index.demo
2. Record: python tools/web_trial/record.py WORK/calls.json ; python tools/web_trial/record2.py WORK/calls2.json
3. Build: python tools/web_trial/build.py <public export>/src/lecturehub/ui/web WORK  (paths are replaced, leak check)
4. Publish WORK/dist to the lecture-hub-demo repository (GitHub Pages).
