"""Render and verify user-facing release summaries from public-release.json."""
from pathlib import Path
from html import escape, unescape
from html.parser import HTMLParser
import argparse, json, os, re, sys, urllib.request

ROOT=Path(__file__).resolve().parents[1]
LABELS={
 'ru':dict(download='Скачать',source='Исходники',guide='Инструкция',bug='Сообщить об ошибке',setup='Требования и ограничения',start='Первые шаги',files='Файлы приложения',checksum='Контрольные суммы',release='Выпуск',preview='Предварительный выпуск',source_status='Только исходники',catalog='Программы',intro='Приложения для повседневных задач. Выберите программу, проверьте требования и откройте руководство.',all='Все программы',os='Платформа',status='Статус',links='Ссылки',notice='Перед использованием прочитайте руководство и примечания к выпуску.',more='Дополнительные снимки',stack='Стек Telegram Media Sender',caption='Демонстрационный интерфейс; настоящий аккаунт Telegram не подключён.',lc_caption='Опубликованная демонстрация v2.4 с синтетическими данными.',details='Посмотреть интерфейс'),
 'de':dict(download='Herunterladen',source='Quellcode',guide='Anleitung',bug='Fehler melden',setup='Voraussetzungen und Grenzen',start='Erste Schritte',files='App-Dateien',checksum='Prüfsummen',release='Release',preview='Vorabversion',source_status='Nur Quellcode',catalog='Programme',intro='Anwendungen für alltägliche Aufgaben. Programm wählen, Voraussetzungen prüfen und die Anleitung öffnen.',all='Alle Programme',os='Plattform',status='Status',links='Links',notice='Vor der Nutzung die Anleitung und Release-Hinweise lesen.',more='Weitere Ansichten',stack='Technik von Telegram Media Sender',caption='Demo-Oberfläche; kein echtes Telegram-Konto verbunden.',lc_caption='Veröffentlichte Demo v2.4 mit synthetischen Daten.',details='Oberfläche ansehen'),
 'en':dict(download='Download',source='Source',guide='User guide',bug='Report a problem',setup='Requirements and limitations',start='First steps',files='Application files',checksum='Checksums',release='Release',preview='Preview',source_status='Source only',catalog='Applications',intro='Applications for everyday tasks. Choose a program, check its requirements and open the guide.',all='All applications',os='Platform',status='Status',links='Links',notice='Read the guide and release notes before use.',more='More screenshots',stack='Telegram Media Sender technology',caption='Demonstration interface; no real Telegram account connected.',lc_caption='Published v2.4 demonstration with synthetic data.',details='See the interface')}
START='<!-- public-release:start -->'
END='<!-- public-release:end -->'
LANGS=('de','en','ru')
def status(d,l):
    return LABELS[l][{'release':'release','preview':'preview','source':'source_status'}[d['status']]]+' '+d['version']
def actions(d,l):
    t=LABELS[l]
    return [(t['download'] if d['release_url'] else t['source'],d['release_url'] or d['source_url']), (t['guide'],d['help_url'].format(lang=l)),(t['bug'],d['feedback_url'])]
def markdown(d,l):
    t=LABELS[l];out=[d['description'][l],'',f"**{d['platform']} · {status(d,l)}**",'', ' · '.join(f'**[{label}]({url})**' for label,url in actions(d,l)),'',f"**{t['setup']}:** {d['requirements'][l]}",'',f"**{t['start']}:** {d['start'][l]}"]
    if d['downloads']:
        out+=['',f"**{t['files']}:**"]
        assets={a['name']:a for a in d['assets']}
        out+=['',*[f"- [`{name}`]({assets[name]['url']})" for name in d['downloads']]]
        out+=['',f"**{t['checksum']}:** "+' · '.join(f"[`{name}`]({assets[name]['url']})" for name in d['checksums'])]
    return START+'\n'+'\n'.join(out)+'\n'+END
def html_summary(d,l):
    t=LABELS[l];e=escape
    out=[f'<section class="topic release-summary" aria-label="{e(t["start"])}">',f'<p>{e(d["description"][l])}</p>',f'<p><strong>{e(d["platform"])} · {e(status(d,l))}</strong></p>','<p class="release-actions">'+' '.join(f'<a class="release-link" href="{e(url)}">{e(label)}</a>' for label,url in actions(d,l))+'</p>',f'<p><strong>{e(t["setup"])}:</strong> {e(d["requirements"][l])}</p>',f'<p><strong>{e(t["start"])}:</strong> {e(d["start"][l])}</p>']
    if d['downloads']:
        assets={a['name']:a for a in d['assets']}
        out += [f'<h2>{e(t["files"])}</h2>','<ul>']+[f'<li><a href="{e(assets[name]["url"])}"><code>{e(name)}</code></a></li>' for name in d['downloads']]+['</ul>',f'<p><strong>{e(t["checksum"])}:</strong> '+' · '.join(f'<a href="{e(assets[n]["url"])}">{e(n)}</a>' for n in d['checksums'])+'</p>']
    out+=['</section>']
    return START+'\n'+'\n'.join(out)+'\n'+END
CSS='.release-summary code{overflow-wrap:anywhere}.release-actions{display:flex;gap:8px;flex-wrap:wrap}.release-link{display:inline-block;padding:9px 13px;border:1px solid #bdd1ed;border-radius:10px;background:#edf4ff;color:#0753ad;font-weight:650;text-decoration:none}.release-link:focus-visible{outline:3px solid #1677ff;outline-offset:3px}'
def replace_block(text,block):
    if START in text:
        return re.sub(re.escape(START)+'.*?'+re.escape(END),lambda _:block,text,flags=re.S)
    return None
def generated_app(d):
    generated={}
    for l,paths in d['readmes'].items():
        for name in paths:
            text=(ROOT/name).read_text(encoding='utf-8');new=replace_block(text,markdown(d,l))
            if new is None:
                # Keep the detailed documentation, illustrations and rights intact.
                match=re.search(r'^# .+$',text,re.M)
                if not match: raise ValueError('Missing project title: '+name)
                text=re.sub(r'^\[(?:User guide|Benutzerhandbuch|Руководство пользователя)\]\(https://popovantondev\.github\.io/[^\n]+\)\n\n','',text,flags=re.M)
                match=re.search(r'^# .+$',text,re.M)
                new=text[:match.end()]+'\n\n'+markdown(d,l)+text[match.end():]
            generated[name]=new
    for l,name in d['guides'].items():
        text=(ROOT/name).read_text(encoding='utf-8');new=replace_block(text,html_summary(d,l))
        if new is None:
            # Place the install summary after the hero, before screenshots and details.
            hero=re.search(r'<section\b[^>]*class="hero(?: [^"]*)?"[^>]*>.*?</section>',text,re.S)
            if not hero: raise ValueError('Missing guide hero: '+name)
            new=text[:hero.end()]+'\n'+html_summary(d,l)+'\n'+text[hero.end():]
        if CSS not in new: new=new.replace('</style>',CSS+'\n</style>',1)
        generated[name]=new
    return generated
CATALOG_CSS='''*{box-sizing:border-box}body{margin:0;background:#f3f6fb;color:#14253d;font:16px/1.65 system-ui,-apple-system,"Segoe UI",sans-serif}a{color:#0753ad;text-underline-offset:3px}a:focus-visible,button:focus-visible{outline:3px solid #1677ff;outline-offset:3px}.shell{max-width:1100px;margin:auto;padding:24px 20px 48px}.top{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}nav{display:flex;gap:8px}nav a{padding:5px 10px;border:1px solid #dbe4f0;border-radius:20px;text-decoration:none;background:white}nav a[aria-current]{background:#e5efff;font-weight:700}.hero{margin:22px 0;padding:32px;border-radius:24px;color:white;background:linear-gradient(115deg,#07172e,#245faa)}h1{font-size:clamp(30px,5vw,46px);line-height:1.15;margin:0 0 14px}h2{font-size:22px;line-height:1.3;margin:0 0 10px}.hero p{max-width:760px;margin:0}.projects{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.project{background:white;border:1px solid #dbe4f0;border-radius:18px;padding:24px;display:flex;flex-direction:column}.project p{margin:8px 0}.project .description{flex:1}.platform{font-size:14px;color:#526176}.status{display:inline-block;align-self:flex-start;border-radius:8px;padding:3px 8px;background:#e7f3eb;color:#25633b;font-size:13px;font-weight:650}.preview{background:#fff1d7;color:#7d5315}.source{background:#eef0f4;color:#4d5663}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.actions a{padding:8px 12px;background:#edf4ff;border-radius:9px;text-decoration:none;font-weight:650;font-size:14px}.actions a:first-child{background:#0753ad;color:white}.requirements{font-size:14px;color:#526176}footer{margin:25px 0 0;color:#526176;font-size:14px}@media(max-width:650px){.projects{grid-template-columns:1fr}.hero{padding:24px}.project{padding:20px}}'''
def profile_readme(d,l):
    t=LABELS[l];projects=d['projects'];out=['**[Deutsch](README.de.md) · [Русский](README.ru.md) · [English](README.md)**','',f'![popovantondev](assets/header-{l}.svg)','',t['intro'],'',f"**[{t['catalog']}](https://popovantondev.github.io/popovantondev/{'index.html' if l=='en' else 'index-'+l+'.html'})**",'']
    for repo,cap,img in [('TelegramMediaSender',t['caption'],f'docs/images/app-{l}.png'),('LectureCompanion',t['lc_caption'],f'docs/screenshots/app-{l}.png')]:
        p=next(p for p in projects if p['repo']==repo)
        out += [f'## [{p["name"]}]({p["source_url"]})','',p['description'][l],'',f'**{p["platform"]} · {status(p,l)}**','', ' · '.join(f'**[{label}]({url})**' for label,url in actions(p,l)),'',f'<details>\n<summary>{t["details"]}</summary>\n\n![{p["name"]}](https://raw.githubusercontent.com/popovantondev/{repo}/main/{img})\n\n*{cap}*\n\n</details>','']
        if repo=='TelegramMediaSender':out += [f'### {t["stack"]}','','Python · PySide6 / Qt · Telethon · PyInstaller','']
    out += [f'## {t["all"]}','',f'| {t["catalog"]} | {t["os"]} | {t["status"]} | {t["links"]} |','|---|---|---|---|']
    for p in projects:
        out.append(f'| [{p["name"]}]({p["source_url"]}) | {p["platform"]} | {status(p,l)} | '+' · '.join(f'[{label}]({url})' for label,url in actions(p,l))+' |')
    out += ['',t['notice'],'']
    return '\n'.join(out)
def catalog(d,l):
    t=LABELS[l];e=escape
    nav=''.join(f'<a href="{"index.html" if x=="en" else "index-"+x+".html"}"'+(' aria-current="page"' if x==l else '')+f'>{dict(de="Deutsch",en="English",ru="Русский")[x]}</a>' for x in LANGS)
    cards=[]
    for p in d['projects']:
        cards += [f'<article class="project" id="{e(p["repo"])}"><h2><a href="{e(p["source_url"])}">{e(p["name"])}</a></h2><span class="status {p["status"]}">{e(status(p,l))}</span><p class="platform">{e(p["platform"])}</p><p class="description">{e(p["description"][l])}</p><p class="requirements">{e(p["requirements"][l])}</p><div class="actions">'+''.join(f'<a href="{e(url)}">{e(label)}</a>' for label,url in actions(p,l))+'</div></article>']
    return f'<!doctype html>\n<html lang="{l}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="{e(t["intro"])}"><title>popovantondev — {e(t["catalog"])}</title><style>{CATALOG_CSS}</style></head><body><div class="shell"><header class="top"><a href="https://github.com/popovantondev">popovantondev</a><nav aria-label="Language">{nav}</nav></header><main><section class="hero"><h1>{e(t["catalog"])}</h1><p>{e(t["intro"])}</p></section><div class="projects">'+ '\n'.join(cards)+f'</div></main><footer>{e(t["notice"])} · <a href="https://github.com/popovantondev">GitHub</a></footer></div></body></html>\n'
def generated(d):
    if 'projects' not in d:
        pages=generated_app(d)
        for l in LANGS:
            t=LABELS[l]
            headings={'ru':['Изменения','Совместимость','Установка','Ограничения','Контрольные суммы'],'de':['Änderungen','Kompatibilität','Installation','Grenzen','Prüfsummen'],'en':['Changes','Compatibility','Installation','Limitations','Checksums']}[l]
            changes={'ru':'Изменения версии:','de':'Änderungen dieser Version:','en':'Version changes:'}[l]
            pages['docs/release-notes.'+l+'.md']='\n'.join([f'# {d["name"]} · {status(d,l)}','',f'## {headings[0]}','',changes+' '+d['source_url']+'/commits/main/','',f'## {headings[1]}','',d['platform'],'',f'## {headings[2]}','',d['start'][l],'',f'## {headings[3]}','',d['requirements'][l],'',f'## {headings[4]}','',(' · '.join(f'[{n}]({next(a["url"] for a in d["assets"] if a["name"]==n)})' for n in d['checksums']) or t['source_status']),''])
        return pages
    return {**{('README.md' if l=='en' else 'README.'+l+'.md'):profile_readme(d,l) for l in LANGS},**{('index.html' if l=='en' else 'index-'+l+'.html'):catalog(d,l) for l in LANGS}}
class Links(HTMLParser):
    def __init__(self):super().__init__();self.links=[];self.images=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='a' and a.get('href'):self.links.append(a['href'])
        if tag=='img' and a.get('src'):self.images.append(a['src'])
def validate(d,files,online=False,published=False):
    errors=[]
    projects=d.get('projects',[d])
    for p in projects:
        expected='https://popovantondev.github.io/'+p['repo']+'/Guide-{lang}.html'
        if p['help_url']!=expected:errors.append(p['repo']+': guide URL must target localized published HTML')
        if p['feedback_url']!=p['source_url']+'/issues/new/choose':errors.append(p['repo']+': incorrect feedback destination')
        if p['status']=='source' and (p['release_url'] or p['downloads']):errors.append(p['repo']+': source-only project advertises a release')
        if p['tag'] and p['release_url']!=p['source_url']+'/releases/tag/'+p['tag']:errors.append(p['repo']+': release URL/tag mismatch')
        for field in ('description','requirements','start'):
            if set(p[field])!=set(LANGS):errors.append(p['repo']+': incomplete localization '+field)
    for name,expected in files.items():
        text=(ROOT/name).read_text(encoding='utf-8')
        if text!=expected:errors.append(name+': regenerate release summary')
        links=Links();links.feed(text)
        for href in links.links+re.findall(r'(?<!!)\[[^\]]+\]\(([^\s)]+)',text):
            if href.startswith(('#','mailto:')) or '://' in href:continue
            path=(ROOT/name).parent/unescape(href.split('#',1)[0].split('?',1)[0])
            if not path.exists():errors.append(name+': missing local link '+href)
        images=links.images+re.findall(r'!\[[^\]]*\]\(([^\s)]+)',text)
        for src in images:
            if src.startswith('https://raw.githubusercontent.com/popovantondev/'+d['repo']+'/main/'):
                src=src.split('/main/',1)[1];path=ROOT/src
            elif '://' not in src:path=(ROOT/name).parent/src
            else:continue
            if not path.is_file():errors.append(name+': missing image '+src)
        if name.endswith('.html'):
            l=re.search(r'<html[^>]*lang="([^"]+)"',text).group(1)
            for other in LANGS:
                target='Guide-'+other+'.html' if 'projects' not in d else ('index.html' if other=='en' else 'index-'+other+'.html')
                if target not in links.links:errors.append(name+': missing language link '+target)
        # Restrict this guard to public descriptions, not functional application prompts.
        visible=unescape(re.sub(r'<[^>]*>',' ',text))
        if re.search(r'(?:made|built|generated|created)\s+(?:with|by|using)\s+(?:ChatGPT|Codex|OpenAI)|(?:сделан\w*|создан\w*|сгенерирован\w*)\s+(?:через|с помощью)\s+(?:ChatGPT|Codex)|(?:erstellt|generiert)\s+(?:mit|durch)\s+(?:ChatGPT|Codex)|imagegen\s+prompt|PORTFOLIO(?:\.|_).*(?:VIDEO|SCENARIO)',visible,re.I):errors.append(name+': internal production content')
    for path in ROOT.rglob('*'):
        if '.git' in path.parts:continue
        if path.name=='AGENTS.md' or (path.is_file() and re.match(r'PORTFOLIO.*\.(?:md|txt)$',path.name,re.I)):errors.append('Internal document: '+str(path.relative_to(ROOT)))
    if 'projects' not in d:
        for item in d.get('version_files',[]):
            text=(ROOT/item['path']).read_text(encoding='utf-8')
            match=re.search(item['pattern'],text,re.M)
            if not match or match.group(1)!=d['version']:errors.append(item['path']+': application/documentation version mismatch')
        if d['tag'] and not d['tag'].startswith('v'+d['version']):errors.append('Release tag/version mismatch')
    if online:
        headers={'Accept':'application/vnd.github+json','User-Agent':'public-documentation-check'}
        if os.environ.get('GITHUB_TOKEN'):headers['Authorization']='Bearer '+os.environ['GITHUB_TOKEN']
        for p in projects:
            if not p['tag']:continue
            url='https://api.github.com/repos/popovantondev/'+p['repo']+'/releases/tags/'+p['tag']
            try:
                with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=30) as response:r=json.load(response)
                if r['draft'] or r['prerelease']!=(p['status']=='preview'):errors.append(p['repo']+': incorrect release status')
                actual={a['name']:a for a in r['assets']}
                for n in p['downloads']+p['checksums']:
                    a=next(a for a in p['assets'] if a['name']==n)
                    if n not in actual or actual[n]['size']!=a['size'] or actual[n]['browser_download_url']!=a['url']:errors.append(p['repo']+': release asset mismatch '+n)
            except Exception as exc:errors.append(p['repo']+': release check failed: '+str(exc))
        if 'projects' in d:
            for p in projects:
                url='https://raw.githubusercontent.com/popovantondev/'+p['repo']+'/main/public-release.json'
                try:
                    with urllib.request.urlopen(url,timeout=30) as response:current=json.load(response)
                    for field in ('version','platform','tag','status','downloads','help_url','feedback_url','description','requirements'):
                        if current[field]!=p[field]:errors.append(p['repo']+': catalog differs from project metadata: '+field)
                except Exception as exc:errors.append(p['repo']+': catalog metadata check failed: '+str(exc))
        if published:
            for p in projects:
                for l in LANGS:
                    url=p['help_url'].format(lang=l)
                    try:
                        with urllib.request.urlopen(url,timeout=30) as response:body=response.read().decode('utf-8')
                        if '<html' not in body.lower() or f'lang="{l}"' not in body:errors.append(url+': not the intended localized HTML page')
                        if p['version'] not in body:errors.append(url+': published version mismatch')
                    except Exception as exc:errors.append(url+': '+str(exc))
    return errors
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--write',action='store_true');parser.add_argument('--online',action='store_true');parser.add_argument('--published',action='store_true');parser.add_argument('--refresh-catalog',action='store_true');args=parser.parse_args()
    d=json.loads((ROOT/'public-release.json').read_text(encoding='utf-8'))
    if args.refresh_catalog:
        if 'projects' not in d:parser.error('--refresh-catalog applies only to the profile repository')
        refreshed=[]
        for p in d['projects']:
            with urllib.request.urlopen('https://raw.githubusercontent.com/popovantondev/'+p['repo']+'/main/public-release.json',timeout=30) as response:refreshed.append(json.load(response))
        d['projects']=refreshed;(ROOT/'public-release.json').write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    files=generated(d)
    if args.write:
        for name,text in files.items():(ROOT/name).write_text(text,encoding='utf-8',newline='\n')
    errors=validate(d,files,args.online,args.published)
    for error in errors:print(error)
    if errors:raise SystemExit(1)
    print(f'Public documentation verified: {len(files)} pages; {len(d.get("projects",[d]))} projects.')
if __name__=='__main__':main()
