.syntax unified
.arm
.section .text
.global _start
_start:
    b start
    .space 188
start:
    ldr r0, =0x04000000
    ldr r1, =0x0403
    strh r1, [r0]
    @ Continuous square wave for the audio test.
    mov r1, #0x80
    strh r1, [r0, #0x84]
    ldr r1, =0x2277
    strh r1, [r0, #0x80]
    mov r1, #2
    strh r1, [r0, #0x82]
    ldr r1, =0xf080
    strh r1, [r0, #0x68]
    ldr r1, =0x8400
    strh r1, [r0, #0x6c]
    ldr r2, =0x06000000
    ldr r3, =0x04000128
    ldr r4, =0x04000130
    ldr r5, =0x04000120
    ldr r6, =0x04000134
    mov r1, #0
    strh r1, [r6]
    ldr r1, =0x2003
    strh r1, [r3]
loop:
    ldrh r1, [r4]
    @ A/B select the four hardware audio rates.
    mvn r8, r1
    and r8, r8, #3
    mov r8, r8, lsl #14
    orr r8, r8, #0x200
    strh r8, [r0, #0x88]
    strh r1, [r2]
    strh r1, [r3, #2]
    mov r8, #6
paint:
    strh r1, [r2, r8]
    add r8, r8, #2
    cmp r8, #480
    blt paint
    ldr r7, =0x0e000000
    strb r1, [r7]
    ldrh r1, [r3]
    tst r1, #0x80
    bne loop
    ldrh r1, [r5]
    strh r1, [r2, #2]
    ldrh r1, [r5, #2]
    strh r1, [r2, #4]
    ldrh r1, [r3]
    tst r1, #4
    bne loop
    tst r1, #8
    beq loop
    orr r1, r1, #0x80
    strh r1, [r3]
    b loop
    .ltorg
    .ascii "SRAM_V113"
