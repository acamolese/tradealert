"""A quale scala temporale il segnale batte il costo? La risposta e' geometrica.

Il costo di un'operazione (lo spread) NON dipende da quanto tieni la posizione.
Il movimento prevedibile invece cresce con il tempo. Quindi esiste una scala
sotto la quale non si puo' guadagnare per costruzione, e una sopra la quale il
finanziamento notturno mangia tutto. In mezzo c'e' la finestra utile.
"""
import json, statistics
from datetime import datetime
from pathlib import Path
CACHE = Path("data/cache")
EPICS = {"US30":0.0038, "DE40":0.0058, "US100":0.0061, "US500":0.0078}
NOTTE = {"US30":0.0216, "DE40":0.0173, "US100":0.0216, "US500":0.0216}

def barre_1m(ep):
    f = CACHE / f"m1_{ep}.json"
    return [x["p"] for x in json.loads(f.read_text())] if f.exists() else []

def barre_15m(ep):
    f = CACHE / f"15m_{ep}.json"
    if not f.exists(): return []
    return [(b["bid"]+b["ask"])/2 for b in json.loads(f.read_text())["barre"]]

def edge(px, L, F):
    res = []
    for i in range(L, len(px)-F, max(L, F)):
        if px[i-L] <= 0 or px[i] <= 0: continue
        p = px[i]/px[i-L] - 1
        if p == 0: continue
        f = (px[i+F]/px[i] - 1) * 100
        res.append(f if p > 0 else -f)
    if len(res) < 50: return None
    m = statistics.mean(res); sd = statistics.stdev(res)
    return abs(m), (abs(m)/(sd/len(res)**0.5) if sd else 0), len(res)

print("Quanto vale il segnale, quanto costa prenderlo, a ogni scala temporale")
print("(media sui quattro indici piu' liquidi; il segnale e' preso in valore")
print(" assoluto: conta quanto e' grande, non da che parte tira)\n")
print(f"{'orizzonte':>12s} {'segnale':>10s} {'costo':>10s} {'rapporto':>10s} {'giudizio':>16s}")
print("-" * 62)
SCALE = [("1 minuto",1,1,"m1",0), ("5 minuti",5,5,"m1",0), ("15 minuti",15,15,"m1",0),
         ("1 ora",4,4,"m15",0), ("4 ore",16,16,"m15",0), ("8 ore",32,32,"m15",0),
         ("1 giorno",64,64,"m15",1), ("3 giorni",192,192,"m15",3), ("5 giorni",320,320,"m15",5)]
for nome, L, F, fonte, notti in SCALE:
    seg, cos, rap = [], [], []
    for ep, sp in EPICS.items():
        px = barre_1m(ep) if fonte == "m1" else barre_15m(ep)
        if len(px) < L + F + 200: continue
        r = edge(px, L, F)
        if not r: continue
        costo = sp + NOTTE[ep] * notti
        seg.append(r[0]); cos.append(costo); rap.append(r[0]/costo)
    if not seg: continue
    ms, mc, mr = statistics.mean(seg), statistics.mean(cos), statistics.mean(rap)
    g = "impossibile" if mr < 1 else ("marginale" if mr < 2 else ("utile" if mr < 4 else "buono"))
    print(f"{nome:>12s} {ms:9.4f}% {mc:9.4f}% {mr:9.2f}x {g:>16s}")
