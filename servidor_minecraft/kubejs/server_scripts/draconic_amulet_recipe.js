ServerEvents.recipes(event => {

    // Intento de requerir dragón nivel 5
    const fireSkull5 = Item.of('iceandfire:dragon_skull_fire', '{Stage:5}')
    const iceSkull5 = Item.of('iceandfire:dragon_skull_ice', '{Stage:5}')
    const lightningSkull5 = Item.of('iceandfire:dragon_skull_lightning', '{Stage:5}')

    const dragonSkullStage5 = Ingredient.of([
        fireSkull5,
        iceSkull5,
        lightningSkull5
    ])

    event.shaped('kubejs:draconic_amulet', [
        'S S',
        'NKW',
        'S S'
    ], {
        S: 'minecraft:string',
        N: 'minecraft:netherite_ingot',
        W: 'minecraft:nether_star',
        K: dragonSkullStage5
    })
})