"""Idempotent navigation and responsive polish for existing HTML guides."""
from pathlib import Path
from html.parser import HTMLParser
import json,re,html
ROOT=Path(__file__).resolve().parents[1]
CSS='''
.guide-tools{box-sizing:border-box;max-width:1120px;margin:16px auto;padding:12px 18px;font:14px/1.6 system-ui,sans-serif;border:1px solid currentColor;border-radius:14px;color:inherit}
.guide-tools nav{display:flex;gap:10px 18px;align-items:center;flex-wrap:wrap;background:none;padding:0;margin:0}
.guide-tools a{display:inline-flex;align-items:center;min-height:44px;color:inherit;background:transparent;text-underline-offset:4px;font-weight:600}
.guide-tools details{margin-top:8px}.guide-tools summary{cursor:pointer;min-height:44px;display:list-item;line-height:44px}.guide-tools ol{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 24px;margin:8px 0;padding-left:22px}.guide-tools li{min-width:0;overflow-wrap:anywhere}
body img{max-width:100%;height:auto}body pre{max-width:100%;overflow:auto}body code{overflow-wrap:anywhere}body table{display:block;max-width:100%;overflow-x:auto}body h1,body h2,body h3,body p,body li{overflow-wrap:anywhere}
a:focus-visible,button:focus-visible,summary:focus-visible,input:focus-visible{outline:3px solid #e88930;outline-offset:4px}
.guide-full-image{display:block;max-width:100%;text-align:center}.guide-full-image img{object-fit:contain}
@media(max-width:760px){.guide-tools{margin:12px 14px;padding:10px 14px}.guide-tools ol{grid-template-columns:1fr}body .top{flex-wrap:wrap}body .hero figure{display:block!important}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto!important}*,*:before,*:after{animation:none!important;transition:none!important}}
'''
COPY={'ru':['Все программы','О программе','Содержание','Открыть изображение целиком'],'de':['Alle Programme','Über die Anwendung','Inhalt','Vollständiges Bild öffnen'],'en':['All applications','About the application','Contents','Open the full image']}

def full_images(source,label):
 class Images(HTMLParser):
  def __init__(self):super().__init__(convert_charrefs=False);self.stack=[];self.edits=[];self.lines=source.splitlines(keepends=True)
  def handle_starttag(self,tag,attrs):
   attrs=dict(attrs)
   if tag=='img':
    src=attrs.get('src','');classes=attrs.get('class','')+' '+' '.join(c for _,c in self.stack)
    if not any(t=='a' for t,_ in self.stack) and not re.search(r'(screen-stack|screen-dark|screen-light|preview-dark|preview-light|popup-preview|app-frame)',classes) and re.search(r'(screenshot|guide-(ru|de|en)|interface|demo-|app-(ru|de|en)|main-window|Ui-|control-|recordings-|surface-laptop|filters-)',src,re.I):
     line,col=self.getpos();start=sum(map(len,self.lines[:line-1]))+col;tagtext=self.get_starttag_text()
     replacement='<a class="guide-full-image" href="'+html.escape(src,quote=True)+'" aria-label="'+label+'">'+tagtext+'</a>'
     self.edits.append((start,start+len(tagtext),replacement))
   if tag not in ('img','input','br','hr','meta','link','source','wbr','area','base','embed','param'):self.stack.append((tag,attrs.get('class','')))
  def handle_endtag(self,tag):
   for i in range(len(self.stack)-1,-1,-1):
    if self.stack[i][0]==tag:self.stack=self.stack[:i];break
 p=Images();p.feed(source)
 for a,b,text in reversed(p.edits):source=source[:a]+text+source[b:]
 return source

def main():
 paths=list(ROOT.glob('Guide-*.html'))+list((ROOT/'docs').glob('Guide-*.html'))+list((ROOT/'website').glob('Guide-*.html'))
 for path in paths:
  s=path.read_text(encoding='utf-8');lang=path.stem[-2:]
  if lang not in COPY:continue
  # Keep release-summary generator output in sync when removing self links.
  start=s.find('<!-- public-release:start -->');end=s.find('<!-- public-release:end -->')
  if start>=0 and end>start:
   part=s[start:end]
   part=re.sub(r'<a\b[^>]*href="[^"]*Guide-'+lang+r'\.html"[^>]*>.*?</a>','',part,flags=re.S)
   part=re.sub(r'(<h2) id="section-\d+"',r'\1',part)
   s=s[:start]+part+s[end:]
  s=re.sub(r'<!-- guide-polish:start -->.*?<!-- guide-polish:end -->\s*','',s,flags=re.S)
  s=re.sub(r'<link rel="stylesheet" href="guide-polish.css(?:\?[^"]*)?">','',s)
  headings=[]
  release_start=s.find('<!-- public-release:start -->');release_end=s.find('<!-- public-release:end -->')
  def heading(m):
   if release_start>=0 and release_start<m.start()<release_end:return m[0]
   attrs,title=m.group(1),m.group(2);label=html.unescape(re.sub('<[^>]+>','',title)).strip()
   if not label:return m[0]
   existing=re.search(r'\bid="([^"]+)"',attrs)
   key=existing[1] if existing else 'section-'+str(len(headings)+1)
   if not existing:attrs+=' id="'+key+'"'
   headings.append((key,label));return '<h2'+attrs+'>'+title+'</h2>'
  s=re.sub(r'<h2\b([^>]*)>(.*?)</h2>',heading,s,flags=re.S)
  cat='index.html' if lang=='en' else 'index-'+lang+'.html';t=COPY[lang]
  # Published guides use relative homepages only where those pages exist.
  home=path.parent/f'index-{lang}.html'
  url=home.name if home.exists() else 'https://popovantondev.github.io/'+ROOT.name+'/index-'+lang+'.html'
  nav=f'<aside class="guide-tools"><nav aria-label="Navigation"><a href="https://popovantondev.github.io/popovantondev/{cat}">← {t[0]}</a><a href="{url}">{t[1]}</a></nav>'
  has_contents=bool(re.search(r'class="[^"]*(?:toc|guide-nav|contents)[^"]*"',s))
  if len(headings)>3 and not has_contents:
   nav+='<details><summary>'+t[2]+'</summary><ol>'+''.join('<li><a href="#'+html.escape(k)+'">'+html.escape(v)+'</a></li>' for k,v in headings)+'</ol></details>'
  nav+='</aside>'
  s=s.replace('</head>','<link rel="stylesheet" href="guide-polish.css?v=20261022"></head>',1)
  s=re.sub(r'(<body\b[^>]*>)\s*',lambda m:m[1]+'\n<!-- guide-polish:start -->'+nav+'<!-- guide-polish:end -->\n',s,count=1)
  s=full_images(s,t[3])
  path.write_text(s,encoding='utf-8')
  (path.parent/'guide-polish.css').write_text(CSS,encoding='utf-8')

if __name__=='__main__':main()
