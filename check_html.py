import sys
from html.parser import HTMLParser

VOID = {"area","base","br","col","embed","hr","img","input","link","meta","param","source","track","wbr"}

class P(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.errs = []
    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append((tag, self.getpos()))
    def handle_startendtag(self, tag, attrs):
        pass
    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.errs.append(f"extra </{tag}> at {self.getpos()}")
            return
        top, pos = self.stack.pop()
        if top != tag:
            self.errs.append(f"mismatch: <{top}> opened {pos} closed by </{tag}> at {self.getpos()}")

path = sys.argv[1]
src = open(path, encoding="utf-8").read()
p = P()
p.feed(src)
p.close()
print("unclosed:", [(t, pos) for t, pos in p.stack])
print("errors:", p.errs[:20])
print("css link:", './style.css' in src, "| js link:", './app.js' in src)
