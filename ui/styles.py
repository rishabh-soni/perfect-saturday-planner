"""Small CSS layer over accessible native Streamlit inputs."""
CSS = """
<style>
:root { --ink:#26382a; --muted:#6d746b; --paper:#f7f5ef; --accent:#45634a; }
html, body, [class*="stApp"] { color:var(--ink); }
.stApp { background:var(--paper); }
.stMainBlockContainer { max-width:1200px; padding-top:2rem; padding-bottom:3rem; }
header[data-testid="stHeader"] { background:transparent; }
[data-testid="stToolbar"] { opacity:.55; }
h1,h2,h3 { color:var(--ink); letter-spacing:-.035em; }
p,li { line-height:1.6; }
label p { font-size:.88rem !important; font-weight:550 !important; }
[data-testid="stForm"] { border:none; padding:0; }
[data-testid="stTextInput"] input, [data-testid="stNumberInput"] input,
[data-baseweb="select"]>div, [data-testid="stTimeInput"] input {
    font-size:.9rem; border-radius:9px; }
.st-key-preferences_panel { background:#fffefa; border:1px solid #e1e3d8; border-radius:18px; padding:24px; }
.st-key-preferences_panel [data-testid="stVerticalBlock"] { gap:.85rem; }
[data-testid="stBaseButton-primary"] { border-radius:10px; padding:.7rem 1.2rem; font-weight:650; min-height:48px; }
[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-secondaryFormSubmit"] { border-radius:10px; }
[data-testid="stExpander"] { border-radius:12px; background:#fffefa; }
.nav { display:flex; justify-content:space-between; align-items:center; gap:16px; padding-bottom:22px; border-bottom:1px solid #dde1d4; }
.wordmark { font-size:21px; font-weight:700; letter-spacing:-.7px; }
.sun-mark { color:#cb844f; font-size:28px; vertical-align:middle; margin-right:9px; }
.nav-note { font-size:10px; letter-spacing:2px; color:#6b7768; }
.weekend { font-size:12px; border:1px solid #d9dfd0; border-radius:20px; padding:8px 14px; background:#f0f2e8; }
.hero { display:flex; align-items:center; justify-content:space-between; gap:35px; padding:36px 0 30px; }
.eyebrow { font-size:10px; font-weight:700; letter-spacing:2px; text-transform:uppercase; color:#7c866e; margin-bottom:12px; }
.hero h1 { font-size:48px; line-height:1.08; font-weight:500; margin:0 0 13px; font-family:Georgia,serif; }
.hero h1 em { color:#6f805e; font-weight:400; }
.hero p { font-size:15px; color:var(--muted); margin:0; }
.hero-art { flex:0 0 230px; height:135px; position:relative; overflow:hidden; border-radius:100px 100px 12px 12px;
    background:linear-gradient(145deg,#e4e8d5,#f2e7cf); }
.hero-art .sun { width:60px; height:60px; border-radius:50%; background:#d7a36a; position:absolute; top:18px; left:93px; }
.hero-art .hill { position:absolute; width:300px; height:125px; background:#9ba986; border-radius:50%; top:90px; left:-70px; transform:rotate(-14deg); }
.hero-art .hill.back { background:#c3cbac; top:83px; left:75px; transform:rotate(22deg); }
.panel-title { font-size:21px; font-family:Georgia,serif; margin:0 0 2px; }
.panel-note { color:var(--muted); font-size:13px; margin:0 0 14px; }
.section-label { font-size:10px; letter-spacing:1.8px; color:#77826c; text-transform:uppercase; font-weight:700; margin:5px 0 6px; }
.empty { min-height:580px; border:1px solid #dde1d3; border-radius:18px; background:#f0f2e8; padding:46px 40px; }
.empty .orbit { width:84px; height:84px; display:grid; place-items:center; color:#74895d; font-size:43px; border:1px solid #ccd4bb; border-radius:50%; margin-bottom:24px; }
.empty h2 { font-family:Georgia,serif; font-size:34px; line-height:1.16; font-weight:400; margin:0 0 18px; }
.empty p { font-size:14px; color:#6c7764; max-width:390px; }
.steps { margin-top:35px; border-top:1px solid #d8dfca; padding-top:15px; }
.step { display:flex; align-items:flex-start; gap:14px; margin:20px 0; }
.step-num { font-size:11px; border:1px solid #cbd6bd; border-radius:50%; width:29px; height:29px; display:grid; place-items:center; flex-shrink:0; }
.step strong { font-size:13px; display:block; font-weight:650; }
.step span { color:#79826e; font-size:12px; }
.result-title { font-family:Georgia,serif; font-size:29px; font-weight:400; line-height:1.2; margin:8px 0 12px; }
.result-summary { color:var(--muted); font-size:13px; margin:0 0 16px; }
.stats { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin:18px 0 22px; }
.stat { padding:13px 8px; border-radius:10px; background:#eeefe5; }
.stat b { display:block; font-size:19px; font-weight:600; }
.stat span { display:block; font-size:9px; letter-spacing:.8px; text-transform:uppercase; margin-top:4px; color:#73816a; }
.activity { background:#fffefa; border:1px solid #e0e3d8; border-radius:14px; padding:22px 24px; margin:0 0 5px; }
.activity-top { display:flex; justify-content:space-between; align-items:center; gap:12px; }
.time { font-size:12px; font-weight:650; letter-spacing:.3px; color:#647958; }
.tag { border-radius:20px; background:#edf0e5; color:#667c53; padding:5px 9px; font-size:10px; }
.tag.uncertain { background:#f7ead6; color:#8b6c3e; }
.activity h3 { font-family:Georgia,serif; font-size:24px; font-weight:400; margin:11px 0 2px; }
.address { font-size:11px; color:#7c8376; margin-bottom:14px; }
.description { font-size:14px; margin-bottom:14px; }
.why { padding:12px 14px; background:#f2f4eb; border-radius:8px; color:#627454; font-size:12px; line-height:1.6; }
.why b { display:block; text-transform:uppercase; font-size:9px; letter-spacing:1.3px; margin-bottom:3px; }
.activity-bottom { display:flex; align-items:center; flex-wrap:wrap; gap:12px; font-size:11px; color:#6b7563; margin-top:16px; }
.activity-bottom a { color:#476946; font-weight:600; margin-left:auto; text-decoration:none; }
.route { font-size:11px; color:#7a826f; border-left:1px dashed #bfccae; padding:13px 15px; margin:0 0 0 24px; }
.footer { border-top:1px solid #dfe2d6; padding-top:17px; margin-top:40px; color:#87907d; font-size:10px; letter-spacing:.3px; display:flex; justify-content:space-between; gap:15px; }
@media(max-width:850px) { .hero h1 {font-size:38px;} .hero-art {flex-basis:170px;} .nav-note {display:none;} }
@media(max-width:640px) {
 .stMainBlockContainer {padding:1rem 1rem 2rem;} .hero {padding:26px 0;}
 .hero h1 {font-size:34px;} .hero-art {display:none;} .wordmark {font-size:18px;}
 .weekend {font-size:10px; padding:6px 9px;} .empty {min-height:420px; padding:30px 24px;}
 .st-key-preferences_panel {padding:20px;} .activity {padding:20px;} .stats {grid-template-columns:repeat(2,1fr);}
 .activity-top {align-items:flex-start;} .footer {flex-direction:column;} }
</style>
"""
