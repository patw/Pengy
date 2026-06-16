# Plot Skill

Generates matplotlib charts as PNGs in ~/Pictures, prints path + `<img>` tag to stdout.

```
python make_plot.py -t <type> -d <data> [options]
```

| Arg | Default | Desc |
|-----|---------|------|
| `-t` | required | `line`, `bar`, `scatter`, `pie`, `hist` |
| `-d` | required | JSON string or path to JSON file |
| `--title` | "" | Chart title |
| `--xlabel` | "" | X-axis label |
| `--ylabel` | "" | Y-axis label |
| `-o` | auto | Output filename (in ~/Pictures) |
| `--width` | 8 | Figure width (inches) |
| `--height` | 5 | Figure height (inches) |
| `--dark` | off | Dark theme |
| `--dpi` | 150 | Resolution |

## Data formats

**Line/Scatter (single):** `{"x":[1,2,3],"y":[4,5,6]}` (x optional, auto 0..n)  
**Line/Scatter (multi):** `[{"label":"A","x":[1,2],"y":[3,4]},...]`  
**Bar:** `{"labels":["A","B"],"values":[12,19]}` or `[{"label":"A","value":12},...]`  
**Pie:** Same shape as bar.  
**Hist:** `[1,2,2,3,3,3]` or `{"values":[...],"bins":10}`

## Output
```
~/Pictures/chart_line_1712345678.png
<img src="file://~/Pictures/chart_line_1712345678.png" alt="...">
```

## Examples
```
python make_plot.py -t line -d '{"x":[1,2,3],"y":[10,20,15]}' --title "Sales"
python make_plot.py -t bar -d '{"labels":["Q1","Q2"],"values":[45,62]}' --dark
python make_plot.py -t line -d data.json --xlabel "Threads" --ylabel "ops/s"
```

Deps pre-installed — use `python3` (system Python).
