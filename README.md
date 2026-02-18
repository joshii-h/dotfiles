# dotfiles

chezmoi-managed dotfiles for **Fedora** (primary) and **Gentoo**.

## Managed Files

| File | Template | Notes |
|---|---|---|
| `~/.zshrc` | Yes | `sysup()` per distro (dnf on Fedora, emerge on Gentoo) |
| `~/.zshenv` | Yes | Cargo env — sourced only if `~/.cargo/env` exists |
| `~/.gitconfig` | Yes | delta pager, user/email per distro |
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
