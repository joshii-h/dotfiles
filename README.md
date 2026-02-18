# dotfiles

chezmoi-managed dotfiles for **Fedora** (primary) and **Gentoo**.

## Managed Files

| File | Template | Notes |
|---|---|---|
| `~/.zshrc` | Yes | `sysup()` only on Fedora |
| `~/.zshenv` | No | Cargo env — portable |
| `~/.config/kitty/kitty.conf` | No | Portable |
| `~/.config/starship.toml` | No | Portable |

Additionally, a `run_once_after` script generates **deno completions** via `deno completions zsh` if deno is installed.

## Install

```bash
chezmoi init --apply joshii-h/dotfiles
```

## Prerequisites

- [oh-my-zsh](https://ohmyz.sh/)
- [starship](https://starship.rs/)
- [zoxide](https://github.com/ajeetdsouza/zoxide)
- [FiraCode Nerd Font](https://www.nerdfonts.com/)

## Distro Detection

chezmoi reads `/etc/os-release` to set a `distro` variable (`fedora` or `gentoo`), used in `.zshrc` template conditionals.

```bash
chezmoi data | grep distro
# "distro": "fedora"
```
