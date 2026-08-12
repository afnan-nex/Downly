# Downly

## Run in CMD
```cmd
aria2c >nul 2>&1 || choco upgrade aria2 -y --install-if-not-installed && curl -L -o Downly.py https://raw.githubusercontent.com/afnan-nex/Downly/main/Downly.py && python -m pip install customtkinter aria2p pillow && python Downly.py

```