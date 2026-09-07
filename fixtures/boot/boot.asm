BITS 16
ORG 0x7C00

start:
    cli
    xor ax, ax
    mov ds, ax
    mov es, ax
    mov ss, ax
    mov sp, 0x7C00
    sti
    call serial_init
    mov si, ready_msg
    call puts
    xor bx, bx

command_loop:
    call getc
    cmp al, 'P'
    je pass
    cmp al, 'F'
    je fail
    cmp al, 'C'
    je crash
    cmp al, 'H'
    je hang
    cmp al, 'I'
    je increment
    cmp al, 'B'
    je seeded_bug
    cmp al, 'Q'
    je shutdown
    jmp command_loop

pass:
    mov si, pass_msg
    call puts
    jmp command_loop

fail:
    mov si, fail_msg
    call puts
    jmp command_loop

crash:
    mov si, crash_msg
    call puts
    ud2
    jmp $

hang:
    mov si, hang_msg
    call puts
    cli
.hang_loop:
    hlt
    jmp .hang_loop

increment:
    inc bl
    mov si, state_msg
    call puts
    mov al, bl
    add al, '0'
    call putc
    mov al, 13
    call putc
    mov al, 10
    call putc
    jmp command_loop

seeded_bug:
    mov si, bug_msg
    call puts
    mov al, '5'
    call putc
    mov al, 13
    call putc
    mov al, 10
    call putc
    jmp command_loop

shutdown:
    mov si, shutdown_msg
    call puts
    mov dx, 0x604
    mov ax, 0x2000
    out dx, ax
    cli
    hlt

serial_init:
    mov dx, 0x3F9
    xor al, al
    out dx, al
    mov dx, 0x3FB
    mov al, 0x80
    out dx, al
    mov dx, 0x3F8
    mov al, 1
    out dx, al
    mov dx, 0x3F9
    xor al, al
    out dx, al
    mov dx, 0x3FB
    mov al, 3
    out dx, al
    mov dx, 0x3FA
    mov al, 0xC7
    out dx, al
    mov dx, 0x3FC
    mov al, 0x0B
    out dx, al
    ret

putc:
    push ax
.wait:
    mov dx, 0x3FD
    in al, dx
    test al, 0x20
    jz .wait
    pop ax
    mov dx, 0x3F8
    out dx, al
    ret

puts:
    lodsb
    test al, al
    jz .done
    call putc
    jmp puts
.done:
    ret

getc:
    mov dx, 0x3FD
.wait:
    in al, dx
    test al, 1
    jz .wait
    mov dx, 0x3F8
    in al, dx
    ret

ready_msg db 'OSLAB_EVT {"event":"READY","seq":0}', 13, 10, 0
pass_msg db 'OSLAB_EVT {"event":"PASS","seq":1}', 13, 10, 0
fail_msg db 'OSLAB_EVT {"event":"FAIL","seq":1}', 13, 10, 0
crash_msg db 'OSLAB_EVT {"event":"CRASH","seq":1}', 13, 10, 0
hang_msg db 'OSLAB_EVT {"event":"HANG_ARMED","seq":1}', 13, 10, 0
state_msg db 'OSLAB_STATE ', 0
bug_msg db 'OSLAB_BUG_VALUE ', 0
shutdown_msg db 'OSLAB_EVT {"event":"SHUTDOWN","seq":2}', 13, 10, 0

times 510-($-$$) db 0
dw 0xAA55
