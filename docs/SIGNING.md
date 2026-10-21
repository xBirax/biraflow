# Signing the installer

Windows shows a warning ("unknown publisher") for a downloaded executable that is not
signed by a certificate the PC trusts. There are three ways to deal with it, from free to
paid.

| Option | Cost | What the user sees |
|---|---|---|
| **SignPath Foundation** (open source) | Free | "Published by ADRAGENCIACRM" — no warning |
| Microsoft Store | Free (15% cut) | The Store's own signing |
| Self-signed certificate (what the build does today) | Free | A warning until the user trusts the certificate once |
| A commercial code-signing certificate | From ~US$ 100/year | No warning |

## The self-signed certificate (default)

`scripts\build-installer.ps1` creates one the first time it runs:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\new-signing-cert.ps1
```

It generates (all outside git, in `packaging\signing\`):

| File | What |
|---|---|
| `biraflow-codesign.pfx` | The private key. **Never share it. Back it up.** |
| `pfx-password.txt` | The password of the .pfx, random |
| `packaging\biraflow-codesign.cer` | The public certificate (can be shared) |

The executables and the installer are signed with SHA-256 and a free timestamp server, so the
signature survives the expiry of the certificate.

Each PC has to trust the certificate once, as an administrator (Win+R):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\trust-signing-cert.ps1
```

Until then, Windows SmartScreen warns about the unknown publisher.

**The automatic updates only accept an installer signed with that same certificate** (the
thumbprint is pinned in `biraflow\_build_info.py`). If the .pfx is lost, the automatic updates
stop working and users have to install the new version by hand.

## SignPath Foundation (free for open source)

SignPath signs the artifacts of open source projects for free. The certificate is publicly
trusted, so **no warning on any PC** and no "trust this certificate" step for the user.

How to apply (one time, by the maintainer):

1. **Make the repository public** on GitHub (the project must be open source and the license
   must be one SignPath accepts — MIT is fine).
2. **Apply** at <https://about.signpath.io/opensource/>: give the repository URL, the licence
   and a short description of the project.
3. SignPath reviews the application (a few days) and creates an **organization** and a
   **policy** for the project. The policy says what can be signed: for Bira Flow, the
   installer `.exe` and the two executables in `dist\BiraFlow\`.
4. Create the **artifact configuration** (which files, how they are named) and copy the
   **configuration UUID**.
5. Add the secrets to the repository (*Settings → Secrets and variables → Actions*):
   `SIGNPATH_TOKEN`, `SIGNPATH_ORGANIZATION`, `SIGNPATH_POLICY` (the configuration UUID) and
   `SIGNPATH_ARTIFACT`.

The release workflow (`.github\workflows\release.yml`) then builds the installer **unsigned**
and signs it with the official action:

```yaml
- name: Sign with SignPath
  uses: signpath/github-action@v1
  with:
    configuration-uuid: ${{ env.SIGNPATH_POLICY }}
    organization-id: ${{ env.SIGNPATH_ORGANIZATION }}
    token: ${{ env.SIGNPATH_TOKEN }}
    artifact-configuration: ${{ env.SIGNPATH_ARTIFACT }}
    files-folder: dist
    output-folder: dist/signed
```

6. **Pin the certificate** in the build so the automatic updates only accept it:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-installer.ps1 `
    -Repo user/bira-flow -SignerThumbprint <impressao digital do certificado da SignPath>
```

Without `-SignerThumbprint`, the build uses the local .pfx. The thumbprint of the SignPath
certificate is shown by the action and by `Get-AuthenticodeSignature` on the signed file.

## Building without any signature

For tests and for a quick local build:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-installer.ps1 -Unsigned
```

The installer works, but the automatic update refuses to install it ("esta versão não tem
certificado fixado"), because there is no certificate to pin.
