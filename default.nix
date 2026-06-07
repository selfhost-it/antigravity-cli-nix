# Questo file permette a chi non usa i Flakes (come il NUR) di accedere al pacchetto.
# Antigravity CLI è un binario closed-source di Google (unfree), quindi importiamo
# nixpkgs con allowUnfree abilitato di default.
{ pkgs ? import <nixpkgs> { config.allowUnfree = true; } }:

{
  # Esponiamo il pacchetto antigravity-cli usando callPackage sul file esistente.
  antigravity-cli = pkgs.callPackage ./package.nix { };
}
