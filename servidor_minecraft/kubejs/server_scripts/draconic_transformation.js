ItemEvents.rightClicked('kubejs:draconic_amulet', event => {

    const player = event.player
    const data = player.persistentData

    // -----------------------------
    // SHIFT + CLICK = seleccionar dragón
    // -----------------------------
    if (player.shiftKeyDown) {

        let type = data.getString('vxw_dragon_type')

        if (type == '' || type == 'forest') {
            type = 'cave'
        } else if (type == 'cave') {
            type = 'sea'
        } else if (type == 'sea') {
            type = 'forest'
        }

        data.putString('vxw_dragon_type', type)

        let nombre = type

        if (type == 'cave') nombre = 'CAVE DRAGON'
        if (type == 'sea') nombre = 'SEA DRAGON'
        if (type == 'forest') nombre = 'FOREST DRAGON'

        player.tell('§5[Amuleto Dracónico] §fForma guardada: §d' + nombre)

        player.runCommandSilent(
            'playsound minecraft:block.amethyst_block.resonate player @s ~ ~ ~ 0.7 1.3'
        )

        event.cancel()
        return
    }


    // -----------------------------
    // CLICK NORMAL = transformación
    // -----------------------------

    let type = data.getString('vxw_dragon_type')

    // No elegimos especie todavía
    if (type == '') {
        player.tell('§cPrimero usa Shift + clic derecho para seleccionar tu especie.')
        event.cancel()
        return
    }

    let hidden = data.getBoolean('vxw_dragon_hidden')


    // -----------------------------
    // DRAGÓN -> HUMANO
    // -----------------------------
    if (!hidden) {

        player.runCommandSilent('dragon human')

        data.putBoolean('vxw_dragon_hidden', true)

        player.runCommandSilent(
            'particle minecraft:reverse_portal ~ ~1 ~ 0.5 0.8 0.5 0.05 50 force'
        )

        player.runCommandSilent(
            'particle minecraft:dragon_breath ~ ~1 ~ 0.4 0.7 0.4 0.02 25 force'
        )

        player.runCommandSilent(
            'playsound minecraft:block.respawn_anchor.deplete player @s ~ ~ ~ 0.8 0.8'
        )

        player.runCommandSilent(
            'title @s actionbar {"text":"Forma humana","color":"aqua","italic":true}'
        )


    // -----------------------------
    // HUMANO -> DRAGÓN
    // -----------------------------
    } else {

        player.runCommandSilent('dragon ' + type + ' 1 1 true')

        data.putBoolean('vxw_dragon_hidden', false)

        player.runCommandSilent(
            'particle minecraft:dragon_breath ~ ~1 ~ 0.8 1.0 0.8 0.05 90 force'
        )

        player.runCommandSilent(
            'particle minecraft:flame ~ ~1 ~ 0.6 0.8 0.6 0.03 35 force'
        )

        player.runCommandSilent(
            'playsound minecraft:entity.ender_dragon.growl player @s ~ ~ ~ 0.7 1.1'
        )

        player.runCommandSilent(
            'title @s actionbar {"text":"Tu naturaleza dracónica despierta","color":"dark_purple","bold":true}'
        )
    }

    event.cancel()
})