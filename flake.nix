{
  description = "Nix package for Antigravity CLI (agy) - Google's agentic coding CLI";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    let
      overlay = final: prev: {
        antigravity-cli = final.callPackage ./package.nix { };
      };
    in
    flake-utils.lib.eachDefaultSystem
      (system:
        let
          pkgs = import nixpkgs {
            inherit system;
            overlays = [ overlay ];
            # The Antigravity CLI is a closed-source Google binary (unfree).
            config.allowUnfree = true;
          };
        in
        {
          packages = {
            default = pkgs.antigravity-cli;
            antigravity-cli = pkgs.antigravity-cli;
          };

          apps.default = {
            type = "app";
            program = "${pkgs.antigravity-cli}/bin/agy";
          };

          devShells.default = pkgs.mkShell {
            buildInputs = with pkgs; [
              nixpkgs-fmt
              jq
              curl
            ];
          };
        }) // {
      overlays.default = overlay;
    };
}
