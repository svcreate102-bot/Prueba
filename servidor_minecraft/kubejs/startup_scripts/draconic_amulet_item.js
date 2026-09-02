StartupEvents.registry('item', event => {
    event.create('draconic_amulet')
        .displayName('§5Amuleto Dracónico')
        .tooltip('§7Oculta o libera tu verdadera forma.')
        .tooltip('§dClic derecho: §fTransformarse')
        .tooltip('§bShift + clic derecho: §fElegir forma dracónica')
        .maxStackSize(1)
        .glow(true)
        .modelJson({
            parent: 'minecraft:item/generated',
            textures: {
                layer0: 'kubejs:item/draconic_amulet'
            }
        })
})