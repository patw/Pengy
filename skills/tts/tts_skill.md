# TTS Skill

Uses `spd-say` (speech-dispatcher, preinstalled on Ubuntu).

```
spd-say "text"
spd-say -r 20 -p 10 "faster, higher pitch"     # speed/pitch (-100..+100)
spd-say -t female1 "female voice"
echo "pipe" | spd-say -e
spd-say -L                                      # list voices
```

## Script
`speak.py` is a thin wrapper:
```
python speak.py "text to say"
```
