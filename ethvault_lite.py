#!/usr/bin/env python3
"""
ETHVault Lite — offline-only signer. Three tabs:

    1. Create   generate / restore a wallet -> encrypted keystore
    2. Sign     scan a wallet's ur:eth-sign-request, review it, sign, show the signature QR
    3. Pair     show the pairing QR (EIP-4527) for MetaMask / Rabby / OKX / Bitget / Keystone Nexus

The network is ALWAYS disabled (no --offline flag needed). No RPC, no broadcasting: the
wallet app you pair with (MetaMask etc.) builds the transaction and broadcasts it.

Run:      python ethvault_lite.py
Test:     python vault_core.py          (logic only, no GUI)
Required: pip install eth_account cbor2 rlp
Optional: pip install qrcode pillow            (QR display)
          pip install pyzbar opencv-python     (camera / screen / image QR scanning)
Keep next to this file: vault_core.py  evur.py  ur.py
EDUCATIONAL TOOL. Unaudited. Test on a testnet with tiny amounts first.
"""
import socket, os, threading, queue, time
from datetime import datetime

# ----------------------------- offline hard-lock -----------------------------
# Subclass (not a plain function) so modules imported later that subclass socket.socket (ssl, asyncio…) still work.
class _NoSocket(socket.socket):
    def __init__(self, *a, **k):
        raise RuntimeError("NETWORK DISABLED: ETHVault Lite is offline-only")
def _blocked(*a, **k):
    raise RuntimeError("NETWORK DISABLED: ETHVault Lite is offline-only")
socket.socket = _NoSocket                # type: ignore
socket.create_connection = _blocked      # type: ignore
socket.getaddrinfo = _blocked            # type: ignore

import json
import vault_core as core
import evur

try:
    import qrcode
    from PIL import Image, ImageTk
    HAS_QR = True
except ImportError:
    HAS_QR = False
try:
    import cv2
    from pyzbar import pyzbar as _pyzbar
    HAS_CAM = True
except ImportError:
    HAS_CAM = False
try:                                     # QR from screenshots / screen grab (no camera needed)
    from PIL import Image as _PILImage, ImageGrab as _ImageGrab
    from pyzbar import pyzbar as _pz
    HAS_IMGQR = True
except Exception:
    HAS_IMGQR = False

import tkinter as tk
from tkinter import ttk, messagebox, filedialog


def qr_image(text: str, size=320):
    q = qrcode.QRCode(box_size=8, border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(text); q.make(fit=True)
    return q.make_image(fill_color="black", back_color="white").convert("RGB").resize((size, size))


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("ETHVault Lite [OFFLINE — network blocked]")
        root.geometry("900x700"); root.minsize(820, 620)
        tk.Label(root, bg="#7a1f1f", fg="white", pady=6, font=("Segoe UI", 11, "bold"),
                 text="OFFLINE SIGNER — all network sockets are hard-disabled. QR / file transfer only."
                 ).pack(fill="x")
        self.status = ttk.Label(root, text="Ready.", anchor="w", relief="sunken")
        self.status.pack(fill="x", side="bottom")
        self.nb = ttk.Notebook(root); self.nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.tabs = {}
        for name in ("1. Create Wallet", "2. Sign Tx", "3. Pair (EIP-4527)"):
            f = ttk.Frame(self.nb); self.nb.add(f, text=name); self.tabs[name] = f
        self._create_tab(); self._sign_tab(); self._pair_tab()

    # --------------------------- helpers ---------------------------------
    def set_status(self, msg): self.status.config(text=msg)

    def async_do(self, fn, on_done):
        def worker():
            try: res, err = fn(), None
            except Exception as e: res, err = None, e
            self.root.after(0, lambda: on_done(res, err))
        threading.Thread(target=worker, daemon=True).start()

    def set_text(self, widget, text):
        widget.config(state="normal"); widget.delete("1.0", "end"); widget.insert("1.0", text)

    def show_qr_window(self, frames, title="ETHVault QR"):
        win = tk.Toplevel(self.root); win.title(title); win.geometry("420x520")
        lbl = tk.Label(win); lbl.pack(padx=10, pady=10)
        info = tk.Label(win, text="", font=("Consolas", 9)); info.pack()
        tk.Button(win, text="Close", command=win.destroy).pack(pady=4)
        if not HAS_QR:
            info.config(text="QR libs missing (pip install qrcode pillow).\nCopy the text instead:")
            t = tk.Text(win, height=10, width=46); t.pack(padx=10, pady=6); t.insert("1.0", "\n".join(frames)); return
        imgs = [ImageTk.PhotoImage(qr_image(fr, 360)) for fr in frames]
        state = {"i": 0, "stop": False}
        def tick():
            if state["stop"] or not win.winfo_exists(): return
            i = state["i"] % len(frames)
            lbl.config(image=imgs[i]); info.config(text=f"Frame {i+1}/{len(frames)}")
            state["i"] += 1; win.after(300, tick)
        win.protocol("WM_DELETE_WINDOW", lambda: (state.__setitem__("stop", True), win.destroy()))
        tick(); self._qr_keepalive = imgs

    # ------------------------- TAB 1: CREATE -----------------------------
    def _create_tab(self):
        f = self.tabs["1. Create Wallet"]
        ttk.Label(f, text="Generate a brand-new wallet. Do this ONLY on a device that is never online.",
                  foreground="#a00").grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=8)
        self.pw1 = ttk.Entry(f, show="*"); self.pw2 = ttk.Entry(f, show="*")
        ttk.Label(f, text="Keystore password:").grid(row=1, column=0, sticky="e", padx=8, pady=4)
        self.pw1.grid(row=1, column=1, sticky="we", padx=8, pady=4)
        ttk.Label(f, text="Repeat password:").grid(row=2, column=0, sticky="e", padx=8, pady=4)
        self.pw2.grid(row=2, column=1, sticky="we", padx=8, pady=4)
        ttk.Button(f, text="Generate (random key)", command=lambda: self.create_wallet(False)
                   ).grid(row=3, column=0, pady=12, padx=8, sticky="we")
        ttk.Button(f, text="Generate + 24-word seed phrase (BIP-39)", command=lambda: self.create_wallet(True)
                   ).grid(row=3, column=1, pady=12, padx=8, sticky="we")
        ttk.Separator(f, orient="horizontal").grid(row=4, column=0, columnspan=3, sticky="we", pady=8)
        ttk.Label(f, text="Restore from existing seed phrase:").grid(row=5, column=0, columnspan=2, sticky="w", padx=8)
        self.restore_seed = tk.Text(f, height=3, width=70)
        self.restore_seed.grid(row=6, column=0, columnspan=2, sticky="we", padx=8, pady=4)
        ttk.Button(f, text="Restore + encrypt to keystore", command=self.restore_wallet
                   ).grid(row=7, column=0, pady=6, padx=8, sticky="we")
        self.create_out = tk.Text(f, height=12, width=90, state="disabled")
        self.create_out.grid(row=8, column=0, columnspan=3, padx=10, pady=10, sticky="nsew")
        f.columnconfigure(1, weight=1); f.rowconfigure(8, weight=1)

    def _pw(self):
        bad = core.password_problem(self.pw1.get(), self.pw2.get())
        if bad: messagebox.showerror("Password", bad); return None
        return self.pw1.get()

    def _save_keystore(self, addr, ks):
        path = filedialog.asksaveasfilename(defaultextension=".json", initialfile=core.keystore_filename(addr),
                                            filetypes=[("Keystore", "*.json")])
        if path:
            with open(path, "w") as fh: json.dump(ks, fh, indent=1)
        return path

    def create_wallet(self, seed: bool):
        pw = self._pw()
        if not pw: return
        def done(res, err):
            if err: return messagebox.showerror("Error", str(err))
            addr, ks, mnemonic = res
            path = self._save_keystore(addr, ks)
            if not path: return
            out = (f"Wallet created {datetime.now():%Y-%m-%d %H:%M}\nAddress : {addr}\nKeystore: {path}\n\n"
                   "- The keystore contains NO plaintext key. Keep this device + password safe.\n")
            if mnemonic:
                self.show_seed_dialog(mnemonic, addr)
                out += "- A 24-word seed phrase was shown (and is NOT saved anywhere).\n"
            self.set_text(self.create_out, out); self.create_out.config(state="disabled")
            self.pw1.delete(0, "end"); self.pw2.delete(0, "end")
            self.set_status(f"Wallet created: {addr}")
        self.async_do(lambda: core.create_keystore(pw, seed), done)

    def show_seed_dialog(self, mnemonic: str, addr: str):
        win = tk.Toplevel(self.root); win.title("SEED PHRASE — write it down NOW"); win.geometry("640x340")
        tk.Label(win, text="Write these 24 words on paper. Never photograph, copy or store them digitally.",
                 fg="#a00", font=("Segoe UI", 10, "bold")).pack(pady=8)
        t = tk.Text(win, height=6, width=72, font=("Consolas", 12)); t.pack(padx=12); t.insert("1.0", mnemonic)
        tk.Label(win, text=f"Address: {addr}").pack(pady=6)
        def close(): t.delete("1.0", "end"); win.destroy()
        tk.Button(win, text="I have written it down — wipe from screen", command=close,
                  bg="#2e7d32", fg="white").pack(pady=8)

    def restore_wallet(self):
        pw = self._pw()
        if not pw: return
        words = core.clean_words(self.restore_seed.get("1.0", "end"))
        if not core.check_seed_length(words):
            return messagebox.showerror("Seed", "Enter a valid 12/15/18/21/24-word seed phrase.")
        def done(res, err):
            if err: return messagebox.showerror("Error", str(err))
            addr, ks = res
            path = self._save_keystore(addr, ks)
            if not path: return
            self.restore_seed.delete("1.0", "end"); self.pw1.delete(0, "end"); self.pw2.delete(0, "end")
            self.create_out.config(state="normal")
            self.create_out.insert("end", f"Restored {addr}\nKeystore saved: {path}\n")
            self.create_out.config(state="disabled")
        self.async_do(lambda: core.restore_keystore(words, pw), done)

    # --------------------------- TAB 2: SIGN -----------------------------
    def _sign_tab(self):
        f = self.tabs["2. Sign Tx"]
        ttk.Label(f, text="Load the wallet's request QR (ur:eth-sign-request), review it, sign with your keystore.",
                  foreground="#a00").grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=6)
        ttk.Label(f, text="Request (ur:eth-sign-request…):").grid(row=1, column=0, sticky="w", padx=10)
        self.s_payload = tk.Text(f, height=5); self.s_payload.grid(row=2, column=0, columnspan=3, padx=10, pady=4, sticky="we")
        ttk.Button(f, text="Scan QR with camera", command=self.sign_scan_camera).grid(row=3, column=0, sticky="w", padx=10)
        ttk.Button(f, text="Load QR image(s)…", command=self.sign_load_images).grid(row=3, column=1, sticky="w", padx=10)
        ttk.Button(f, text="Scan QR from screen", command=self.sign_scan_screen).grid(row=3, column=2, sticky="w", padx=10)
        ttk.Label(f, text="Keystore file:").grid(row=4, column=0, sticky="w", padx=10, pady=(10, 0))
        self.s_ks = ttk.Entry(f); self.s_ks.grid(row=5, column=0, columnspan=2, sticky="we", padx=10, pady=2)
        ttk.Button(f, text="Browse…", command=self.sign_pick_ks).grid(row=5, column=2, sticky="w", padx=10)
        ttk.Label(f, text="Keystore password:").grid(row=6, column=0, sticky="w", padx=10)
        self.s_pw = ttk.Entry(f, show="*"); self.s_pw.grid(row=6, column=1, sticky="we", padx=10, pady=2)
        ttk.Button(f, text="REVIEW & SIGN", command=self.sign_tx).grid(row=7, column=0, columnspan=3, pady=12)
        self.s_out = tk.Text(f, height=14); self.s_out.grid(row=8, column=0, columnspan=3, padx=10, pady=6, sticky="nsew")
        f.columnconfigure(1, weight=1); f.rowconfigure(8, weight=1)

    def _got_request(self, text):
        self.s_payload.delete("1.0", "end"); self.s_payload.insert("1.0", text.strip())
        self.set_status("Request scanned.")

    def sign_pick_ks(self):
        p = filedialog.askopenfilename(filetypes=[("Keystore", "*.json"), ("All", "*.*")])
        if p: self.s_ks.delete(0, "end"); self.s_ks.insert(0, p)

    @staticmethod
    def _qr_texts(img):
        return [d.data.decode("utf-8", "ignore") for d in _pz.decode(img)]

    @staticmethod
    def _feed(urdec, texts):
        """Feed decoded QR strings into a URDecoder; True when the UR is complete."""
        for t in texts:
            if t.strip()[:3].lower() == "ur:" and urdec.add(t): return True
        return False

    def sign_scan_camera(self):
        if not HAS_CAM:
            return messagebox.showinfo("Camera unavailable", "Install:  pip install pyzbar opencv-python\n"
                                       "Or use 'Load QR image(s)' / 'Scan QR from screen' / paste the text.")
        win = tk.Toplevel(self.root); win.title("Scan QR (animated QR supported)"); win.geometry("520x460")
        vid = tk.Label(win); vid.pack(padx=8, pady=8)
        info = tk.Label(win, text="Starting camera…", font=("Consolas", 9)); info.pack()
        tk.Button(win, text="Cancel", command=win.destroy).pack(pady=4)
        q = queue.Queue(); urdec = evur.URDecoder()
        def grab():
            cap = cv2.VideoCapture(0)
            try:
                while win.winfo_exists():
                    ok, frame = cap.read()
                    if not ok: time.sleep(0.1); continue
                    q.put(("frame", frame.copy()))
                    for d in _pyzbar.decode(frame): q.put(("code", d.data.decode()))
                    time.sleep(0.03)
            except Exception as e:
                q.put(("err", str(e)))
            finally:
                cap.release()
        def poll():
            if not win.winfo_exists(): return
            try:
                while True:
                    kind, item = q.get_nowait()
                    if kind == "frame":
                        im = Image.fromarray(cv2.cvtColor(item, cv2.COLOR_BGR2RGB)).resize((460, 320))
                        poll.ph = ImageTk.PhotoImage(im); vid.config(image=poll.ph)
                    elif kind == "code":
                        try:
                            if self._feed(urdec, [item]):
                                t, body = urdec.result(); win.destroy()
                                self._got_request(evur.ur_encode_single(t, body)); return
                            n, tot = urdec.progress(); info.config(text=f"UR fragments: {n}/{tot}")
                        except Exception as e:
                            info.config(text=f"UR scan: {e}")
                    elif kind == "err":
                        info.config(text=f"Camera error: {item}")
            except queue.Empty:
                pass
            self.root.after(50, poll)
        threading.Thread(target=grab, daemon=True).start(); poll()

    def sign_load_images(self):
        if not HAS_IMGQR:
            return messagebox.showinfo("QR from image", "Install:  pip install pyzbar pillow")
        paths = filedialog.askopenfilenames(title="Select QR screenshot(s) — all frames if animated",
                                            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp"), ("All", "*.*")])
        if not paths: return
        urdec = evur.URDecoder()
        try:
            for p in paths:
                if self._feed(urdec, self._qr_texts(_PILImage.open(p).convert("RGB"))): break
        except Exception as e:
            return messagebox.showerror("QR from image", str(e))
        if urdec.done:
            t, body = urdec.result(); self._got_request(evur.ur_encode_single(t, body))
        else:
            n, tot = urdec.progress()
            messagebox.showwarning("QR from image", f"Incomplete: got {n}/{tot} fragments." if tot else
                "No readable QR found. Crop tighter / make the QR bigger.\nFor an animated QR select screenshots "
                "of ALL its frames at once, or use 'Scan QR from screen'.")

    def sign_scan_screen(self):
        """Grabs the screen ~7x/second and decodes any QR on it (works with animated QR codes)."""
        if not HAS_IMGQR:
            return messagebox.showinfo("Scan from screen", "Install:  pip install pyzbar pillow")
        win = tk.Toplevel(self.root); win.title("Scan QR from screen"); win.geometry("340x110")
        win.attributes("-topmost", True)
        info = tk.Label(win, text="Show the wallet's QR on screen\n(don't cover it with this window)", justify="left")
        info.pack(padx=10, pady=8)
        tk.Button(win, text="Cancel", command=win.destroy).pack()
        urdec = evur.URDecoder(); q = queue.Queue()
        def grab():
            while win.winfo_exists():
                try: q.put(("codes", self._qr_texts(_ImageGrab.grab(all_screens=True).convert("RGB"))))
                except Exception as e: q.put(("err", str(e)))
                time.sleep(0.15)
        def poll():
            if not win.winfo_exists(): return
            try:
                while True:
                    kind, item = q.get_nowait()
                    if kind == "err": info.config(text=f"Error: {item}"); continue
                    try:
                        if self._feed(urdec, item):
                            t, body = urdec.result(); win.destroy()
                            self._got_request(evur.ur_encode_single(t, body)); return
                    except Exception as e:
                        info.config(text=f"UR: {e}"); continue
                    if item: n, tot = urdec.progress(); info.config(text=f"UR fragments: {n}/{tot}")
            except queue.Empty:
                pass
            self.root.after(100, poll)
        threading.Thread(target=grab, daemon=True).start(); poll()

    def sign_tx(self):
        text = self.s_payload.get("1.0", "end").strip()
        if text[:3].lower() != "ur:":
            return messagebox.showerror("Request", "Paste or scan a ur:eth-sign-request from your wallet app.")
        try:
            rq, tx, review, _known = core.review_request(text)
        except Exception as e:
            return messagebox.showerror("Request error", str(e))
        if not messagebox.askyesno("Confirm signing", review + "\nSign this transaction?"):
            return
        ks_path = self.s_ks.get().strip(); pw = self.s_pw.get()
        if not ks_path or not os.path.exists(ks_path):
            return messagebox.showerror("Keystore", "Choose the keystore file.")
        def done(res, err):
            if err: return messagebox.showerror("Signing failed", f"{err}\n(Wrong password?)")
            frames, addr = res
            self.s_out.delete("1.0", "end")
            self.s_out.insert("1.0", f"SIGNED ✔  signer: {addr}\nShow the QR to the wallet app — it broadcasts the "
                                     f"transaction.\n\n{frames[0].lower()}\n")
            self.s_pw.delete(0, "end")
            self.show_qr_window(frames, "SIGNATURE → scan with your wallet app")
            self.set_status("Signed. Scan the signature QR with the requesting app.")
        self.set_status("Decrypting keystore (scrypt)…")
        self.async_do(lambda: core.sign_request(rq, tx, core.load_keystore(ks_path), pw), done)

    # --------------------------- TAB 3: PAIR -----------------------------
    def _pair_tab(self):
        f = self.tabs["3. Pair (EIP-4527)"]
        ttk.Label(f, foreground="#a00", justify="left", text=(
            "Show your PUBLIC account key as a QR so MetaMask / Rabby / OKX / Bitget / Keystone Nexus can watch this\n"
            "wallet and send it transactions to sign. Needs the seed phrase (a keystore holds only the private key).")
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=8)
        ttk.Label(f, text="Seed phrase:").grid(row=1, column=0, sticky="ne", padx=8)
        self.p_seed = tk.Text(f, height=3, width=70); self.p_seed.grid(row=1, column=1, sticky="we", padx=8, pady=4)
        ttk.Label(f, text="BIP-39 passphrase (optional):").grid(row=2, column=0, sticky="e", padx=8)
        self.p_pass = ttk.Entry(f, show="*"); self.p_pass.grid(row=2, column=1, sticky="we", padx=8, pady=4)
        ttk.Label(f, text="Pair with:").grid(row=3, column=0, sticky="e", padx=8)
        self.p_kind = ttk.Combobox(f, state="readonly", width=52, values=[c[0] for c in core.PAIR_CHOICES])
        self.p_kind.current(0); self.p_kind.grid(row=3, column=1, sticky="w", padx=8, pady=4)
        ttk.Button(f, text="Show pairing QR", command=self.pair_show).grid(row=4, column=1, sticky="w", padx=8, pady=8)
        self.p_out = tk.Text(f, height=12); self.p_out.grid(row=5, column=0, columnspan=2, padx=10, pady=8, sticky="nsew")
        f.columnconfigure(1, weight=1); f.rowconfigure(5, weight=1)

    def pair_show(self):
        words = core.clean_words(self.p_seed.get("1.0", "end")); pp = self.p_pass.get()
        if not core.check_seed_length(words):
            return messagebox.showerror("Seed", "Enter a valid 12/15/18/21/24-word seed phrase.")
        choice = self.p_kind.current()
        def done(res, err):
            if err: return messagebox.showerror("Error", str(err))
            frames, addr, xfp = res
            self.p_seed.delete("1.0", "end"); self.p_pass.delete(0, "end")        # wipe seed from screen
            self.p_out.delete("1.0", "end")
            self.p_out.insert("1.0", f"First account (m/44'/60'/0'/0/0): {addr}\nMaster fingerprint: {xfp}\n\n{core.PAIR_HELP}")
            self.show_qr_window(frames, "PAIRING QR → scan with your wallet app")
        self.async_do(lambda: core.pairing_frames(words, pp, choice), done)


if __name__ == "__main__":
    root = tk.Tk()
    try: ttk.Style().theme_use("clam")
    except Exception: pass
    App(root)
    root.mainloop()
