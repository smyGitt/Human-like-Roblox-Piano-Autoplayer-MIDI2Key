# Midi2Key

MIDI2Key includes various humanization options for a natural, human-like playback. Including automatic pedal timing and 88-key keyboard support. Easy to run via executable or source code, with GUI support included.


<img width="326" height="396" alt="Midi2Key v1.3 Playback tab" src="https://github.com/user-attachments/assets/c9d39ad9-0517-4ea6-acb3-da312868aaed" />
<img width="326" height="396" alt="Midi2Key v1.3 Visualizer tab" src="https://github.com/user-attachments/assets/dfe54071-70ba-4c80-b2a8-3c866f3f7cb1" />
<img width="326" height="396" alt="image" src="https://github.com/user-attachments/assets/17b5eac5-b194-4d77-af0e-f1d1c17e463a" />



# How do you run it?
### If you downloaded the .exe from the releases:
  just run the .exe! 

### Or, if you don't trust me...
You can create your own `.exe` with pyinstaller. I've provided the icon `icon.ico`, so use with this command:
    
    pyinstaller --noconsole --onefile --icon=icon.ico --add-data "icon.ico;." main.py

  make sure the .ico file and .py file is in the same directory.

### ...if you choose to do neither above:
You need to run this in a command prompt. After you navigate to where the .py files are. type for example:

    python main.py
    
  else if you are not going to use the main.py with GUI, for whatever reason, navigate to the backup folder, open command prompt there, then enter:

    python final_beforeGUI.py --help

  (of course, you may replace final_beforeGUI.py with noDuration.py, or any in the backup if desired)

this will show you all the available flags and formatting. Though I can't guarantee the same quality from the GUI versions, because the noGUI version hasn't been updated at all.

Remember that it accepts .mid files only. it works best with piano-only .mid, but I've seen it work with mixed instruments.

# Dependencies
you might need to install a few python libraries though. You will see that main.py imports various libraries.

    import mido, time, headpq, threading, random, copy, numpy, sys, dataclasses, import, collections, os, PyQt6  

and it may differ between the .py files. Please check if you have these installed.

if you get some warning about pyinstaller not being compatible with some module, like `typing`, then just uninstall it with 

    python -m pip uninstall <MODULE NAME>
