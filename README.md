# dotfiles

chezmoi-managed dotfiles for **Fedora** (primary) and **Gentoo**.

## Managed Files

| File | Template | Notes |
|---|---|---|
| `~/.config/zsh/.zshrc` | Yes | `sysup()` per distro (dnf on Fedora, emerge on Gentoo) |
| `~/.zshenv` | Yes | Cargo env — sourced only if `~/.cargo/env` exists |
| `~/.config/git/config` | Yes | delta pager, user/email per distro |
| `~/.config/git/ignore` | No | Global gitignore |
| `~/.config/gh/config.yml` | No | GitHub CLI config |
| `~/.config/kitty/kitty.conf` | No | Portable |
| `~/.config/starship.toml` | No | Portable |
| `~/.config/btop/btop.conf` | No | Portable |

Additionally, a `run_once_after` script generates **deno completions** via `deno completions zsh` if deno is installed.

## Install

```bash
chezmoi init --apply joshii-h/dotfiles
```

## Prerequisites

### Common

- [oh-my-zsh](https://ohmyz.sh/)
- [starship](https://starship.rs/)
- [zoxide](https://github.com/ajeetdsouza/zoxide)
- [FiraCode Nerd Font](https://www.nerdfonts.com/)

### Fedora

```bash
sudo dnf install zsh zsh-completions starship kitty git-delta zoxide
```

### Gentoo

```bash
# Keyword unmask for zoxide
echo 'app-shells/zoxide ~amd64' | sudo tee /etc/portage/package.accept_keywords/zoxide

sudo emerge app-shells/zsh app-shells/zsh-completions app-shells/starship \
  x11-terms/kitty dev-util/git-delta app-shells/zoxide
```

Then install chezmoi (not in Portage):

```bash
sh -c "$(curl -fsLS get.chezmoi.io)" -- -b ~/.local/bin
```

## Distro Detection

chezmoi reads `/etc/os-release` to set a `distro` variable (`fedora` or `gentoo`), used in template conditionals.

```bash
chezmoi data | grep distro
# "distro": "gentoo"
```

## Windows (dual boot)

`windows/` is not managed by chezmoi (see `.chezmoiignore`). It holds the Windows side of the Battlestation:

| File | Notes |
|---|---|
| `windows/sysup.ps1` | Windows counterpart of `sysup`: winget, Windows Update incl. drivers, Store, Defender, WSL (+ apt in the distros), rustup/npm/scoop/choco, and `git pull` of this repo. Runs jobs in parallel, writes a summary like the Linux version, **never reboots**. |
| `windows/winget-skip.txt` | winget IDs that sysup leaves alone (with reason) |
| `windows/stay-awake.ps1` | Keeps the PC awake and aborts pending shutdowns, for unattended runs |
| `windows/install.ps1` | Symlinks the scripts to `%USERPROFILE%\scripts`, adds `sysup` to the PowerShell profile, blocks automatic restarts after updates, sets the RTC to UTC |

```powershell
git clone ssh://git@git.joshuahirsig.xyz:5478/joshii/dotfiles.git $env:USERPROFILE\Projects\dotfiles
pwsh -File $env:USERPROFILE\Projects\dotfiles\windows\install.ps1   # elevated
sysup                     # everything
sysup -Skip store         # e.g. skip a job
```

Why no automatic restarts: the boot order starts Gentoo, so an unattended reboot would not come back to Windows. The hardware clock is UTC on both systems (`RealTimeIsUniversal` on Windows, `clock="UTC"` in Gentoo's `/etc/conf.d/hwclock`).
