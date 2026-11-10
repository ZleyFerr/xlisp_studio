; Run with Ctrl+Enter
(+ 10 20)

(define (square x)
  (* x x))

(square 12)
(map square '(1 2 3 4 5))

(define (factorial n)
  (if (< n 2)
      1
      (* n (factorial (- n 1)))))

(factorial 6)
(format #t "Hello from XLISP!~%")
