

def man_link(filename):
    fp = open(filename, 'r')
    is_ok = False
    for line in fp.readlines():
        if line.find("4") < 0 and is_ok != True:
            is_ok = True
            continue

        if line.find(".") >= 0:
            line_pre = line
            line_next = fp.readline()
            #print line_pre, line_next
if __name__ == "__main__":
    filename = "/Users/lishuming/projects/github/lishuming.github.com/_posts/2016-06-13-awesome-blogs.md"
    man_link(filename)
