"""banner"""
from __future__ import annotations

ASCII_ART = r"""
           /$$                                              
          | $$                                              
  /$$$$$$ | $$$$$$$   /$$$$$$$  /$$$$$$$  /$$$$$$  /$$$$$$$ 
 /$$__  $$| $$__  $$ /$$_____/ /$$_____/ |____  $$| $$__  $$
| $$  \ $$| $$  \ $$|  $$$$$$ | $$        /$$$$$$$| $$  \ $$
| $$  | $$| $$  | $$ \____  $$| $$       /$$__  $$| $$  | $$
|  $$$$$$$| $$  | $$ /$$$$$$$/|  $$$$$$$|  $$$$$$$| $$  | $$
 \____  $$|__/  |__/|_______/  \_______/ \_______/|__/  |__/
 /$$  \ $$                                                  
|  $$$$$$/                                                  
 \______/                                                   """

BYLINE = "by m4gester-bitt"

FOOTNOTE = (
    "Please don't use this for illgeal purposes ;) "
    "Use it to fix leaks, not to go digging through other people's secrets."
)


def print_banner() -> None:
    print(ASCII_ART)
    print(f"  {BYLINE}")
    print(f"  {FOOTNOTE}\n")
