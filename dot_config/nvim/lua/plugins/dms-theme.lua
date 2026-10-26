-- Farben aus dem Hintergrundbild (DankMaterialShell/matugen erzeugt
-- colors/dms.lua und lua/lualine/themes/dms.lua). Ohne DMS (z.B. Fedora)
-- greift tokyonight, das LazyVim-Standardschema.
return {
  -- base46: Grundlage für das DMS-Schema (colors/dms.lua lädt es)
  { "duxlabs-dev/base46", lazy = true, opts = {} },
  {
    "LazyVim/LazyVim",
    opts = {
      colorscheme = function()
        -- colors/dms.lua gibt bei fehlendem base46 nur eine Meldung aus,
        -- darum zusätzlich prüfen, ob das Schema wirklich aktiv wurde
        local ok = pcall(vim.cmd.colorscheme, "dms")
        if not ok or vim.g.colors_name ~= "dms" then
          vim.cmd.colorscheme("tokyonight")
        end
      end,
    },
  },
  {
    "nvim-lualine/lualine.nvim",
    opts = function(_, opts)
      if pcall(require, "lualine.themes.dms") then
        opts.options = opts.options or {}
        opts.options.theme = "dms"
      end
    end,
  },
}
