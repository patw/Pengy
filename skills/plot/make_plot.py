#!/usr/bin/env python3
"""Generate matplotlib charts as PNGs for web embedding. Types: line, bar, scatter, pie, hist."""
import argparse, json, os, sys, time
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

def parse_data(raw):
    p = Path(raw)
    if p.exists(): raw = p.read_text()
    return json.loads(raw)

def _line(data, ax, a):
    if isinstance(data,list) and all(isinstance(d,dict) for d in data):
        for s in data: ax.plot(s.get("x",range(len(s["y"]))), s["y"], label=s.get("label"), marker="o", ms=3)
        ax.legend()
    else: ax.plot(data.get("x",range(len(data["y"]))), data["y"], marker="o", ms=3)

def _bar(data, ax, a):
    if isinstance(data,dict): lbl,val = data["labels"],data["values"]
    else: lbl = [d["label"] for d in data]; val = [d["value"] for d in data]
    ax.bar(lbl, val)

def _scatter(data, ax, a):
    if isinstance(data,list) and all(isinstance(d,dict) for d in data):
        for s in data: ax.scatter(s.get("x",range(len(s["y"]))), s["y"], label=s.get("label"), s=20)
        ax.legend()
    else: ax.scatter(data.get("x",range(len(data["y"]))), data["y"], s=20)

def _pie(data, ax, a):
    if isinstance(data,dict): lbl,val = data["labels"],data["values"]
    else: lbl = [d["label"] for d in data]; val = [d["value"] for d in data]
    _,_,at = ax.pie(val, labels=lbl, autopct="%1.1f%%", textprops={"fontsize":9})
    for t in at: t.set_fontweight("bold")

def _hist(data, ax, a):
    if isinstance(data,dict): val,bins = data["values"],data.get("bins","auto")
    else: val,bins = data,"auto"
    ax.hist(val, bins=bins, edgecolor="white", alpha=0.8)
    ax.set_ylabel("Frequency")

FUNCS = {"line":_line,"bar":_bar,"scatter":_scatter,"pie":_pie,"hist":_hist}

def dark_style(fig, ax):
    fig.patch.set_facecolor("#1e1e1e"); ax.set_facecolor("#2d2d2d")
    for s in ["bottom","top","left","right"]: ax.spines[s].set_color("#666")
    ax.tick_params(colors="#ccc"); ax.xaxis.label.set_color("#ccc"); ax.yaxis.label.set_color("#ccc")
    ax.title.set_color("#fff"); ax.grid(color="#444", linestyle="--", alpha=0.5)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("-t","--type", required=True, choices=["line","bar","scatter","pie","hist"])
    p.add_argument("-d","--data", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--xlabel", default="")
    p.add_argument("--ylabel", default="")
    p.add_argument("-o","--output", default="")
    p.add_argument("--width", type=float, default=8)
    p.add_argument("--height", type=float, default=5)
    p.add_argument("--dark", action="store_true")
    p.add_argument("--dpi", type=int, default=150)
    a = p.parse_args()
    try: data = parse_data(a.data)
    except Exception as e: print(f"ERROR: {e}", file=sys.stderr); sys.exit(1)
    out = Path(a.output) if a.output else Path.home() / "Pictures" / f"chart_{a.type}_{int(time.time())}.png"
    if not out.is_absolute(): out = Path.home() / "Pictures" / out
    fig, ax = plt.subplots(figsize=(a.width, a.height))
    if a.dark: dark_style(fig, ax)
    FUNCS[a.type](data, ax, a)
    if a.xlabel: ax.set_xlabel(a.xlabel)
    if a.ylabel: ax.set_ylabel(a.ylabel)
    if a.title: ax.set_title(a.title, fontsize=13, fontweight="bold", pad=10)
    fig.tight_layout(); fig.savefig(out, dpi=a.dpi, bbox_inches="tight"); plt.close(fig)
    print(out); print(f'<img src="file://{out}" alt="{a.title or a.type} chart">')

if __name__=="__main__": main()
