"""Insert a light horizontal rule between every body row of every pandoc longtable."""
import sys, pathlib

def add_rules(tex: str) -> tuple[str, int]:
    buf, added = [], 0
    in_tbl = in_body = False
    for ln in tex.split('\n'):
        if ln.startswith(r'\begin{longtable}'):
            in_tbl, in_body = True, False
            buf.append(ln); continue
        if in_tbl and ln.strip() == r'\endlastfoot':
            in_body = True
            buf.append(ln); continue
        if in_tbl and ln.startswith(r'\end{longtable}'):
            while buf and buf[-1] == r'\rowsep':
                buf.pop(); added -= 1
            buf.append(ln); in_tbl = in_body = False; continue
        buf.append(ln)
        if in_tbl and in_body and ln.rstrip().endswith(r'\\'):
            buf.append(r'\rowsep'); added += 1
    return '\n'.join(buf), added

p = pathlib.Path(sys.argv[1])
t, n = add_rules(p.read_text())
p.write_text(t)
print(f'{p.name}: inserted {n} row rules')
