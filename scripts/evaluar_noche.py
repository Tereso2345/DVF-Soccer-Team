#!/usr/bin/env python3
"""
Noche desatendida (DVF, 2026-11-22, sesión 67): mantiene la PC despierta mientras entrena una
corrida y, cuando termina, la evalúa sola para tener el informe a la mañana.

Por qué: la noche del 20→21/11 el script anti-suspensión tenía hora fija (08:05) y la última fase
siguió hasta las 09:11 (`--until` solo se mira entre fases): la notebook se suspendió y pausó 28 min.
Además la evaluación se hizo a mano a la mañana. Este script resuelve las dos cosas.

  1. Pide a Windows "sistema requerido" (SetThreadExecutionState; no cambia ninguna configuración)
     hasta que termine TODO (entrenamiento + evaluación) o hasta --tope (por seguridad).
  2. Espera a que muera el supervisor (--esperar-pid).
  3. Arma los candidatos: el par al final de cada fase de keeper (= fin de ciclo, sacado de
     checkpoints/), el par final de la corrida y un par de referencia (--ref-striker/--ref-keeper).
  4. Benchmark de cada uno: 1 min + gol de oro, --seeds por lado, contra NormalTeam, v1 y v2.3
     (usa chequeo_completo._benchmark: mismo formato que los chequeos).
  5. Ranking por el neto contra v2.3 (y v1 de desempate). Copia fija del mejor en chequeos/.
  6. Sondas de saque (chequeo_completo._saques) y del keeper (sonda_keeper) para el mejor, el final
     y la referencia: el keeper de corrida6 volvía tarde al arco en el saque (sesión 67).
  7. Informe → models/runs/<run>/chequeos/noche_<fecha>.md

Uso (lo lanza desacoplado scripts/lanzar_noche.ps1, ver su cabecera):
    python scripts/evaluar_noche.py --run corrida7_diego --esperar-pid 12345 \
        --ref-striker models/runs/corrida6_diego/chequeos/20261121_0911_striker.npz \
        --ref-keeper  models/runs/corrida6_diego/chequeos/20261121_0911_keeper.npz
Prueba rápida sobre una corrida terminada: --esperar-pid 0 --seeds 1 --sin-sondas
"""
import argparse
import ctypes
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import chequeo_completo as CC  # noqa: E402  (reutiliza _benchmark y _saques)

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
CK = re.compile(r"^(striker|keeper)_(\d{8}_\d{6})_IT(\d+)_s(\d+)_(striker|keeper)\.npz$")


def _log(f, msg):
	linea = f"{dt.datetime.now():%Y-%m-%dT%H:%M:%S} {msg}"
	print(linea, flush=True)
	with open(f, "a", encoding="utf-8") as fh:
		fh.write(linea + "\n")


def _despierta(si=True):
	# Windows: pedido de "sistema requerido" (como un reproductor de video); se renueva en cada llamada
	if os.name == "nt":
		ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if si else 0))


def _supervisor_vivo(pid):
	if not pid or not psutil.pid_exists(pid):
		return False
	try:
		return "supervisar_entrenamiento" in " ".join(psutil.Process(pid).cmdline())
	except (psutil.NoSuchProcess, psutil.AccessDenied):
		return False


def _candidatos(run_dir):
	"""[(etiqueta, striker, keeper)]: fin de cada fase de keeper, en orden, más el par final."""
	fases = {}   # sello de la fase -> (IT máximo, pasos)
	for p in (run_dir / "checkpoints").glob("*.npz"):
		m = CK.match(p.name)
		if m and m.group(5) == "keeper":
			sello, it, pasos = m.group(2), int(m.group(3)), m.group(4)
			if it > fases.get(sello, (0, ""))[0]:
				fases[sello] = (it, pasos)
	out = []
	for i, sello in enumerate(sorted(fases), 1):
		it, pasos = fases[sello]
		base = f"{sello}_IT{it:04d}_s{pasos}_keeper.npz"
		s, k = run_dir / "checkpoints" / f"striker_{base}", run_dir / "checkpoints" / f"keeper_{base}"
		if s.exists() and k.exists():
			out.append((f"fase keeper {i} ({sello[9:11]}:{sello[11:13]}, IT {it})", s, k))
	out.append(("**final**", run_dir / "striker.npz", run_dir / "keeper.npz"))
	return out


def _sonda_keeper(striker, keeper):
	r = subprocess.run([sys.executable, str(ROOT / "scripts" / "sondas" / "sonda_keeper.py"), str(striker), str(keeper), "180"],
	                   cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
	                   env=dict(os.environ, PYTHONIOENCODING="utf-8", TF_CPP_MIN_LOG_LEVEL="2"))
	return [l for l in r.stdout.splitlines() if l.startswith("marcador")]


def main():
	ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument("--run", required=True)
	ap.add_argument("--esperar-pid", type=int, default=0, help="PID del supervisor (0 = no esperar)")
	ap.add_argument("--ref-striker", type=Path)
	ap.add_argument("--ref-keeper", type=Path)
	ap.add_argument("--seeds", type=int, default=16)
	ap.add_argument("--conf-seeds", type=int, default=48, help="semillas por lado de la confirmación contra v2.3")
	ap.add_argument("--workers", type=int, default=10)
	ap.add_argument("--tope", default="", help="hora ISO en la que se libera el pedido de no suspender pase lo que pase")
	ap.add_argument("--sin-sondas", action="store_true")
	args = ap.parse_args()

	run_dir = ROOT / "models" / "runs" / args.run
	out_dir = run_dir / "chequeos"
	out_dir.mkdir(parents=True, exist_ok=True)
	log = run_dir / "evaluar_noche.log"
	tope = dt.datetime.fromisoformat(args.tope) if args.tope else None
	try:   # prioridad baja; los procesos hijos (benchmark, sondas) la heredan
		psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
	except Exception:
		pass

	try:
		_despierta(True)
		_log(log, f"inicio: run={args.run} esperando supervisor {args.esperar_pid} | tope {tope}")
		while _supervisor_vivo(args.esperar_pid):
			if tope and dt.datetime.now() > tope:
				_log(log, "!!! se pasó el tope esperando el entrenamiento: libero la PC sin evaluar")
				return
			_despierta(True)
			time.sleep(60)
		_log(log, "entrenamiento terminado: empiezo la evaluación")
		t0 = time.time()

		cands = _candidatos(run_dir)
		if args.ref_striker and args.ref_keeper:
			cands.insert(0, ("ref", args.ref_striker.resolve(), args.ref_keeper.resolve()))
		res = []
		for etiqueta, s, k in cands:
			if tope and dt.datetime.now() > tope:
				_log(log, "!!! se pasó el tope durante la evaluación: corto acá"); break
			_despierta(True)
			filas, _ = CC._benchmark(s, k, args.seeds, 60, args.workers)
			res.append((etiqueta, s, k, filas))
			_log(log, f"{etiqueta}: " + " | ".join(f"{r} {f['gep']} ({f['neto']:+.1f})" for r, f in filas.items()))

		# Ranking: neto contra v2.3, desempate con v1 (NormalTeam no decide: premia trucos)
		evaluables = [r for r in res if r[0] != "ref" and "v2.3" in r[3]]
		evaluables.sort(key=lambda r: (r[3]["v2.3"]["neto"], r[3].get("v1", {}).get("neto", -99)), reverse=True)
		sello = dt.datetime.now().strftime("%Y%m%d_%H%M")
		md = [f"# Evaluación de la noche — {args.run} ({dt.datetime.now():%d/%m %H:%M})", "",
		      f"1 min + gol de oro, {args.seeds} semillas × 2 lados = {2 * args.seeds} partidos por rival (G-E-P y neto por par de partidos).",
		      "Ordenado por el neto contra v2.3. NormalTeam se muestra pero no decide.", "",
		      "| par | NormalTeam | v1 | v2.3 |", "|---|---|---|---|"]

		def celda(f, r):
			return f"{f[r]['gep']} ({f[r]['neto']:+.1f} ±{f[r]['ic']:.1f})" if r in f else "—"
		ref = [r for r in res if r[0] == "ref"]
		for etiqueta, s, k, f in ref + evaluables:
			nombre = f"ref (`{s.name}`)" if etiqueta == "ref" else etiqueta
			md.append(f"| {nombre} | {celda(f, 'NormalTeam')} | {celda(f, 'v1')} | {celda(f, 'v2.3')} |")
		md.append("")

		if evaluables:
			# Confirmación (como a mano el 21/11): con 16 semillas el ganador puede ser ruido, así que los
			# 3 mejores + la referencia juegan 48 semillas por lado contra v2.3 y se elige con eso.
			finalistas = evaluables[:3] + ref
			md += [f"## Confirmación contra v2.3: {args.conf_seeds} semillas × 2 lados = {2 * args.conf_seeds} partidos", "",
			       "| par | G-E-P | neto/2 part. |", "|---|---|---|"]
			conf = []
			for etiqueta, s, k, _ in finalistas:
				_despierta(True)
				f = CC._benchmark(s, k, args.conf_seeds, 60, args.workers, rivales=["soccer_team_DVF_v2"])[0].get("v2.3")
				if f:
					conf.append((f["neto"], etiqueta, s, k))
					md.append(f"| {etiqueta} | {f['gep']} | {f['neto']:+.1f} ±{f['ic']:.1f} |")
					_log(log, f"confirmación {etiqueta}: v2.3 {f['gep']} ({f['neto']:+.1f})")
			md.append("")
			conf_dl = sorted([c for c in conf if c[1] != "ref"], key=lambda c: c[0], reverse=True)
			mejor = next(r for r in evaluables if conf_dl and r[0] == conf_dl[0][1]) if conf_dl else evaluables[0]
			ms, mk = out_dir / f"noche_{sello}_mejor_striker.npz", out_dir / f"noche_{sello}_mejor_keeper.npz"
			shutil.copy2(mejor[1], ms); shutil.copy2(mejor[2], mk)
			md += [f"**Mejor contra v2.3: {mejor[0]}** → copia fija `chequeos/{ms.name}` + `_keeper`.", ""]
			cref = [c for c in conf if c[1] == "ref"]
			if cref and conf_dl:
				d = conf_dl[0][0] - cref[0][0]
				md += [f"Neto contra v2.3 del mejor menos el de la referencia (confirmación): **{d:+.1f}**. "
				       "Si es menor que el ± de la tabla, no hay mejora clara.", ""]

			if not args.sin_sondas:
				vistos, sondear = set(), []
				for etiqueta, s, k, _ in [mejor] + [r for r in res if r[0] == "**final**"] + ref:
					if str(s) not in vistos:
						vistos.add(str(s)); sondear.append((etiqueta, s, k))
				md += ["## Saques y keeper (el keeper de corrida6 volvía tarde al arco: x≈305 a los 2 s; corrida5 ≈281)", ""]
				for etiqueta, s, k in sondear:
					_despierta(True)
					_log(log, f"sondas: {etiqueta}")
					md += [f"### {etiqueta}", "", "```"] + CC._saques(s, k) + _sonda_keeper(s, k) + ["```", ""]

		md.append(f"_Evaluación hecha en {(time.time() - t0) / 60:.1f} min con {args.workers} procesos._")
		informe = out_dir / f"noche_{sello}.md"
		informe.write_text("\n".join(md) + "\n", encoding="utf-8")
		_log(log, f"listo: {informe}")
	finally:
		_despierta(False)   # libera el pedido: la PC vuelve a su configuración normal


if __name__ == "__main__":
	main()
